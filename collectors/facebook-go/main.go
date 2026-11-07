// A bounded, read-only adapter around facebook-go's single-group feed API.
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/teslashibe/facebook-go/groups"
)

type collectorError struct {
	Code    string `json:"code"`
	Stage   string `json:"stage"`
	Message string `json:"message"`
	Page    int    `json:"page,omitempty"`
	Post    int    `json:"post_index,omitempty"`
}

type envelope struct {
	SchemaVersion  int               `json:"schema_version"`
	Collector      string            `json:"collector"`
	Source         string            `json:"source"`
	LibraryCommit  string            `json:"library_commit"`
	RequestedPages int               `json:"requested_pages"`
	PageSize       int               `json:"page_size"`
	Sort           string            `json:"sort"`
	GroupID        string            `json:"group_id"`
	RunID          string            `json:"run_id"`
	StartedAt      string            `json:"started_at"`
	CollectedAt    string            `json:"collected_at"`
	DurationMS     int64             `json:"duration_ms"`
	PagesFetched   int               `json:"pages_fetched"`
	Status         string            `json:"status"`
	Errors         []collectorError  `json:"errors"`
	Posts          []json.RawMessage `json:"posts"`
	Window         *windowInfo       `json:"window"`
}

type windowInfo struct {
	From                  string `json:"from"`
	To                    string `json:"to"`
	Complete              bool   `json:"complete"`
	StopReason            string `json:"stop_reason"`
	ScannedPosts          int    `json:"scanned_posts"`
	UnknownTimestampPosts int    `json:"unknown_timestamp_posts"`
	OrderingViolations    int    `json:"ordering_violations"`
}

type config struct {
	Group, Output, RunID, DebugDir string
	Pages, PageSize                int
	Timeout                        time.Duration
	From, To                       time.Time
}

type groupClient interface {
	ListGroupPosts(context.Context, string, string, groups.PostListOptions) (groups.FeedPage, error)
}

type clientFactory func(context.Context, groups.Cookies) (groupClient, error)

var numericID = regexp.MustCompile(`^[0-9]+$`)

// Do not echo the supplied input: mistaken input can contain credentials.
func parseGroup(input string) (string, error) {
	s := strings.TrimSpace(input)
	if numericID.MatchString(s) {
		return s, nil
	}
	u, err := url.Parse(s)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.User != nil || u.Port() != "" {
		return "", errors.New("provide a numeric group ID or a Facebook /groups/<numeric-ID>/ URL")
	}
	switch strings.ToLower(u.Hostname()) {
	case "facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "mbasic.facebook.com":
	default:
		return "", errors.New("group URL must use a Facebook hostname")
	}
	parts := strings.Split(strings.Trim(u.Path, "/"), "/")
	if len(parts) != 2 || parts[0] != "groups" || !numericID.MatchString(parts[1]) {
		return "", errors.New("group URL must contain a numeric group ID; use the ID for vanity URLs")
	}
	return parts[1], nil
}

func parseCookies(raw, expectedUser string) (groups.Cookies, error) {
	if strings.ContainsAny(raw, "\r\n") {
		return groups.Cookies{}, errors.New("FACEBOOK_COOKIE must be a single Cookie header without newlines")
	}
	req := &http.Request{Header: http.Header{}}
	req.Header.Set("Cookie", raw)
	jar := make(map[string]string)
	for _, c := range req.Cookies() {
		if _, duplicate := jar[c.Name]; duplicate {
			return groups.Cookies{}, errors.New("FACEBOOK_COOKIE contains duplicate cookie names")
		}
		jar[c.Name] = c.Value
	}
	if !numericID.MatchString(jar["c_user"]) || jar["xs"] == "" {
		return groups.Cookies{}, errors.New("FACEBOOK_COOKIE requires c_user and xs from a valid session")
	}
	if expectedUser != "" && expectedUser != jar["c_user"] {
		return groups.Cookies{}, errors.New("FACEBOOK_USER_ID does not match the session's c_user")
	}
	return groups.Cookies{CUser: jar["c_user"], XS: jar["xs"], Jar: jar}, nil
}

func classify(err error, stage string, page int) collectorError {
	e := collectorError{Code: "unknown_error", Stage: stage, Page: page, Message: "unclassified collector failure; inspect library compatibility"}
	var netErr net.Error
	var syntaxErr *json.SyntaxError
	var typeErr *json.UnmarshalTypeError
	switch {
	case errors.Is(err, groups.ErrInvalidAuth), errors.Is(err, groups.ErrUnauthorized), errors.Is(err, groups.ErrSenderIdentityMismatch):
		e.Code, e.Message = "authentication_failed", "session authentication failed or account identity does not match"
	case errors.Is(err, groups.ErrSessionExpired):
		e.Code, e.Message = "session_expired", "session expired; refresh it in a browser before retrying"
	case errors.Is(err, groups.ErrCheckpoint):
		e.Code, e.Message = "checkpoint_required", "Facebook requires a checkpoint; collection stopped"
	case errors.Is(err, groups.ErrRateLimited), errors.Is(err, groups.ErrFeatureBlocked):
		e.Code, e.Message = "rate_limited", "Facebook rate limited or temporarily blocked collection; collection stopped"
	case errors.Is(err, groups.ErrAccountRestricted):
		e.Code, e.Message = "account_restricted", "Facebook restricted this account; collection stopped without retry"
	case errors.Is(err, groups.ErrForbidden), errors.Is(err, groups.ErrNotMember):
		e.Code, e.Message = "group_not_accessible", "group is not accessible to this session"
	case errors.Is(err, groups.ErrNotFound):
		e.Code, e.Message = "group_not_found", "Facebook reported that the group was not found"
	case errors.Is(err, groups.ErrDocIDStale), errors.As(err, &syntaxErr), errors.As(err, &typeErr):
		e.Code, e.Message = "parse_error", "response cannot be parsed or Facebook query identifiers are stale"
	case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded), errors.As(err, &netErr):
		e.Code, e.Message = "network_error", "request failed, was cancelled, or exceeded the time budget"
	case errors.Is(err, groups.ErrRequestFailed):
		// Upstream wraps JSON decode failures in ErrRequestFailed without %w.
		// Inspect only for classification; never copy its text to logs or output.
		s := strings.ToLower(err.Error())
		if strings.Contains(s, "decod") || strings.Contains(s, "unmarshal") || strings.Contains(s, "has no group") || strings.Contains(s, "no data in response") {
			e.Code, e.Message = "parse_error", "Facebook returned an unexpected or malformed feed response"
		} else {
			e.Code, e.Message = "network_error", "Facebook request failed or returned an unexpected HTTP status"
		}
	}
	return e
}

func newClient(ctx context.Context, cookies groups.Cookies) (groupClient, error) {
	// The stdlib transport preserves inherited HTTP(S)_PROXY and system CA trust.
	// Upstream disables automatic redirects and checks manual bootstrap redirects.
	var transport http.RoundTripper = http.DefaultTransport
	if capture, ok := ctx.Value(captureKey{}).(*rawCapture); ok {
		transport = &captureTransport{Base: transport, Capture: capture}
	}
	return groups.NewContext(ctx, cookies,
		groups.WithHTTPClient(&http.Client{Timeout: 30 * time.Second, Transport: transport}),
		groups.WithRetry(1, 0),
	)
}

func safeFetch(ctx context.Context, c groupClient, user, group string, opts groups.PostListOptions) (page groups.FeedPage, err error) {
	defer func() {
		if recover() != nil {
			err = &json.UnmarshalTypeError{Value: "upstream parser panic"}
		}
	}()
	return c.ListGroupPosts(ctx, user, group, opts)
}

func collect(ctx context.Context, cfg config, cookies groups.Cookies, factory clientFactory, result *envelope, log io.Writer) {
	capture := &rawCapture{Stories: map[string]*storyEvidence{}, DebugDir: cfg.DebugDir}
	for name, value := range cookies.Jar {
		if name != "c_user" && name != "ps_l" && name != "ps_n" && (len(value) >= 4 || name == "xs") {
			capture.Secrets = append(capture.Secrets, value)
		}
	}
	ctx = captureContext(ctx, capture)
	if result.Window != nil {
		result.Window.StopReason = "error"
	}
	c, err := factory(ctx, cookies)
	if err != nil {
		result.Errors = append(result.Errors, classify(err, "bootstrap", 0))
		return
	}
	cursor := ""
	seenCursors := map[string]bool{}
	seenPosts := map[string]bool{}
	var previousTime time.Time
	for pageNo := 1; pageNo <= cfg.Pages; pageNo++ {
		capture.PageStoryCount = 0
		capture.InvalidCursor = false
		capture.FeedSeen = false
		capture.PageInfoSeen = false
		page, err := safeFetch(ctx, c, cookies.CUser, result.GroupID, groups.PostListOptions{
			Sort: groups.SortChronological, Cursor: cursor, Count: cfg.PageSize,
		})
		if err != nil {
			result.Errors = append(result.Errors, classify(err, "fetch", pageNo))
			break
		}
		result.PagesFetched++
		if capture.PageStoryCount > len(page.Posts) {
			result.Errors = append(result.Errors, collectorError{Code: "parse_error", Stage: "raw_story_count", Message: "upstream parser omitted one or more story nodes", Page: pageNo})
		}
		accepted := 0
		allOlder := len(page.Posts) > 0
		for i, post := range page.Posts {
			// Isolate serialization failures to one post; preserve all other posts.
			if result.Window != nil {
				result.Window.ScannedPosts++
				if post.CreatedAt.IsZero() || post.CreatedAt.Year() <= 1 {
					result.Window.UnknownTimestampPosts++
					allOlder = false
				} else {
					if !post.CreatedAt.Before(cfg.From) {
						allOlder = false
					}
					if !seenPosts[post.ID] {
						if !previousTime.IsZero() && post.CreatedAt.After(previousTime) {
							result.Window.OrderingViolations++
						}
						previousTime = post.CreatedAt
					}
				}
			}
			seenPosts[post.ID] = true
			raw, err := json.Marshal(post)
			if err != nil {
				allOlder = false
				result.Errors = append(result.Errors, collectorError{Code: "parse_error", Stage: "post", Message: "post cannot be serialized", Page: pageNo, Post: i + 1})
				continue
			}
			var enriched map[string]any
			if json.Unmarshal(raw, &enriched) != nil {
				continue
			}
			enriched = sanitize(enriched, capture.Secrets).(map[string]any)
			enriched["image_candidates"] = []imageCandidate{}
			enriched["possible_type"] = "unknown"
			enriched["type_evidence"] = []string{}
			enriched["debug_response_saved"] = false
			enriched["debug_file"] = nil
			evidence := capture.Stories[post.ID]
			if evidence == nil {
				evidence = capture.Stories[post.StoryID]
			}
			if evidence != nil {
				enriched["image_candidates"] = evidence.Images
				enriched["possible_type"] = evidence.PossibleType
				enriched["type_evidence"] = evidence.TypeEvidence
				if evidence.DebugFile != "" {
					enriched["debug_file"] = evidence.DebugFile
					enriched["debug_response_saved"] = true
				}
			}
			raw, _ = json.Marshal(enriched)
			result.Posts = append(result.Posts, raw)
			accepted++
		}
		fmt.Fprintf(log, "[facebook-go] page=%d posts=%d\n", pageNo, accepted)
		if result.Window != nil && capture.FeedSeen && !capture.PageInfoSeen {
			result.Errors = append(result.Errors, collectorError{Code: "invalid_cursor", Stage: "pagination", Message: "group feed omitted pagination metadata; exhaustion cannot be established", Page: pageNo})
			result.Window.StopReason = "invalid_cursor"
			break
		}
		if capture.InvalidCursor {
			result.Errors = append(result.Errors, collectorError{Code: "invalid_cursor", Stage: "pagination", Message: "Facebook signaled another page without a usable cursor", Page: pageNo})
			if result.Window != nil {
				result.Window.StopReason = "invalid_cursor"
			}
			break
		}
		if !page.HasNext {
			if result.Window != nil {
				result.Window.StopReason = "feed_exhausted"
				result.Window.Complete = true
			}
			break
		}
		if result.Window != nil && allOlder && result.Window.OrderingViolations == 0 {
			result.Window.StopReason = "older_than_from"
			result.Window.Complete = true
			break
		}
		if page.NextCursor == "" || seenCursors[page.NextCursor] {
			result.Errors = append(result.Errors, collectorError{Code: "pagination_stalled", Stage: "pagination", Message: "missing or repeated pagination cursor; collection stopped", Page: pageNo})
			break
		}
		seenCursors[page.NextCursor] = true
		cursor = page.NextCursor
		if result.Window != nil {
			result.Window.StopReason = "page_limit"
		}
	}
	if capture.Err {
		result.Errors = append(result.Errors, collectorError{Code: "media_parse_error", Stage: "debug", Message: "sanitized debug evidence could not be saved"})
	}
	if result.Window != nil {
		if result.Window.UnknownTimestampPosts > 0 || result.Window.OrderingViolations > 0 || len(result.Errors) > 0 {
			result.Window.Complete = false
		}
		fmt.Fprintf(log, "[facebook-go] window_complete=%t stop_reason=%s scanned=%d unknown_timestamps=%d ordering_violations=%d\n", result.Window.Complete, result.Window.StopReason, result.Window.ScannedPosts, result.Window.UnknownTimestampPosts, result.Window.OrderingViolations)
	}
}

func writeJSON(path string, value any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".collector-*.json")
	if err != nil {
		return err
	}
	name := f.Name()
	defer os.Remove(name)
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	if err := enc.Encode(value); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(name, path)
}

func generatedRunID() string {
	var b [12]byte
	if _, err := rand.Read(b[:]); err == nil {
		return hex.EncodeToString(b[:])
	}
	return time.Now().UTC().Format("20060102T150405.000000000Z")
}

func run(args []string, getenv func(string) string, stderr io.Writer, factory clientFactory) int {
	start := time.Now()
	cfg := config{}
	var timeoutSeconds float64
	var fromText, toText string
	fs := flag.NewFlagSet("facebook-go", flag.ContinueOnError)
	// Flag errors can include accidental credentials; only print our safe error.
	fs.SetOutput(io.Discard)
	fs.StringVar(&cfg.Group, "group", getenv("FACEBOOK_GROUP_ID"), "numeric Facebook group ID or group URL")
	fs.StringVar(&cfg.Output, "output", "data/raw/facebook-go.json", "raw JSON output")
	fs.StringVar(&cfg.RunID, "run-id", "", "shared benchmark run ID")
	fs.IntVar(&cfg.Pages, "pages", 2, "maximum pages / window safety cap (1..200)")
	fs.IntVar(&cfg.PageSize, "page-size", 20, "posts per page (1..20)")
	fs.Float64Var(&timeoutSeconds, "timeout", 120, "total request budget in seconds")
	fs.StringVar(&fromText, "from", "", "inclusive lower timestamp, RFC3339 with timezone")
	fs.StringVar(&toText, "to", "", "inclusive upper timestamp, RFC3339 with timezone")
	fs.StringVar(&cfg.DebugDir, "debug-dir", "", "private per-run sanitized GraphQL story directory")
	parseErr := fs.Parse(args)
	windowErr := false
	if fromText != "" || toText != "" {
		var fromErr, toErr error
		cfg.From, fromErr = time.Parse(time.RFC3339Nano, fromText)
		cfg.To, toErr = time.Parse(time.RFC3339Nano, toText)
		windowErr = fromErr != nil || toErr != nil || cfg.From.After(cfg.To)
	}
	if timeoutSeconds > 0 && timeoutSeconds <= 3600 && !math.IsNaN(timeoutSeconds) && !math.IsInf(timeoutSeconds, 0) {
		cfg.Timeout = time.Duration(timeoutSeconds * float64(time.Second))
	}
	if errors.Is(parseErr, flag.ErrHelp) {
		fmt.Fprintln(stderr, "facebook-go --group ID|URL [--pages 2] [--page-size 20] [--timeout 120] [--from RFC3339 --to RFC3339] [--debug-dir DIR] [--output PATH] [--run-id ID]")
		return 0
	}
	if cfg.RunID == "" {
		cfg.RunID = generatedRunID()
	}
	result := envelope{SchemaVersion: 1, Collector: "facebook-go", Source: "live", LibraryCommit: "0c762f3eea9082ed7a4694d6c474c3622546b3b6", RequestedPages: cfg.Pages, PageSize: cfg.PageSize, Sort: "CHRONOLOGICAL", RunID: cfg.RunID, StartedAt: start.UTC().Format(time.RFC3339Nano), Posts: []json.RawMessage{}, Errors: []collectorError{}}
	if !cfg.From.IsZero() && !windowErr {
		result.Window = &windowInfo{From: cfg.From.UTC().Format(time.RFC3339Nano), To: cfg.To.UTC().Format(time.RFC3339Nano), StopReason: "error"}
	}
	groupID, groupErr := parseGroup(cfg.Group)
	result.GroupID = groupID
	switch {
	case parseErr != nil || fs.NArg() != 0:
		result.Errors = append(result.Errors, collectorError{Code: "unknown_error", Stage: "configuration", Message: "invalid CLI options; use --help"})
	case groupErr != nil:
		result.Errors = append(result.Errors, collectorError{Code: "unknown_error", Stage: "configuration", Message: groupErr.Error()})
	case cfg.Pages < 1 || cfg.Pages > 200 || cfg.PageSize < 1 || cfg.PageSize > 20 || cfg.Timeout <= 0 || windowErr:
		result.Errors = append(result.Errors, collectorError{Code: "unknown_error", Stage: "configuration", Message: "pages must be 1..200; page-size 1..20; timeout 0..3600 seconds; supply both --from/--to RFC3339 in ascending order"})
	default:
		cookies, err := parseCookies(getenv("FACEBOOK_COOKIE"), getenv("FACEBOOK_USER_ID"))
		if err != nil {
			result.Errors = append(result.Errors, collectorError{Code: "authentication_failed", Stage: "configuration", Message: err.Error()})
		} else {
			fmt.Fprintf(stderr, "[facebook-go] starting group=%s\n", groupID)
			ctx, cancel := context.WithTimeout(context.Background(), cfg.Timeout)
			collect(ctx, cfg, cookies, factory, &result, stderr)
			cancel()
		}
	}
	result.CollectedAt = time.Now().UTC().Format(time.RFC3339Nano)
	result.DurationMS = time.Since(start).Milliseconds()
	result.Status = "success"
	if len(result.Errors) > 0 {
		result.Status = "failed"
		if len(result.Posts) > 0 {
			result.Status = "partial"
		}
	}
	if err := writeJSON(cfg.Output, result); err != nil {
		fmt.Fprintln(stderr, "[facebook-go] output_error: cannot write JSON output")
		return 1
	}
	for _, e := range result.Errors {
		fmt.Fprintf(stderr, "[facebook-go] %s stage=%s page=%d: %s\n", e.Code, e.Stage, e.Page, e.Message)
	}
	fmt.Fprintf(stderr, "[facebook-go] total=%d pages=%d duration=%dms status=%s\n", len(result.Posts), result.PagesFetched, result.DurationMS, result.Status)
	if len(result.Errors) > 0 {
		return 1
	}
	return 0
}

func main() { os.Exit(run(os.Args[1:], os.Getenv, os.Stderr, newClient)) }
