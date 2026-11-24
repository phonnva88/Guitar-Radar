package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/teslashibe/facebook-go/groups"
)

var engagementID = regexp.MustCompile(`^[A-Za-z0-9_:/+=-]{1,256}$`)

type engagementInput struct {
	Operation  string `json:"operation"`
	FeedbackID string `json:"feedback_id"`
	PostID     string `json:"post_id"`
	GroupID    string `json:"group_id"`
	Account    string `json:"account_id"`
	Cursor     string `json:"cursor"`
	Message    string `json:"message"`
	Reaction   string `json:"reaction"`
}

type engagementOutput struct {
	Account       string                `json:"account_id,omitempty"`
	Error         string                `json:"error,omitempty"`
	Comments      *[]interactionComment `json:"comments,omitempty"`
	NextCursor    string                `json:"next_cursor,omitempty"`
	Comment       *groups.Comment       `json:"comment,omitempty"`
	Reaction      string                `json:"reaction,omitempty"`
	CommentCount  *int64                `json:"comment_count,omitempty"`
	ReactionCount *int64                `json:"reaction_count,omitempty"`
	PreviewOnly   bool                  `json:"preview_only,omitempty"`
	Unavailable   bool                  `json:"comments_unavailable,omitempty"`
}

type interactionComment struct {
	groups.Comment
	ParentID string `json:"parent_id,omitempty"`
}

type engagementTransport struct {
	base          http.RoundTripper
	headers       http.Header
	feedbackID    string
	commentPage   bool
	reactionSaved bool
	commentCount  *int64
	reactionCount *int64
	rootInput     *engagementInput
	rootPage      *engagementOutput
	cookies       groups.Cookies
}

func (t *engagementTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	rootRequest := false
	if t.rootInput != nil {
		updated, err := commentRootRequest(req, *t.rootInput)
		if err != nil {
			return nil, err
		}
		rootRequest, req = updated != req, updated
	}
	resp, err := t.base.RoundTrip(req)
	if err == nil && resp.StatusCode == http.StatusOK && req.Method == http.MethodGet &&
		req.URL.Scheme == "https" && req.URL.Host == "www.facebook.com" &&
		strings.TrimRight(req.URL.Path, "/") == "/groups/feed" {
		t.headers = req.Header.Clone()
	}
	if err != nil || resp.StatusCode != http.StatusOK || req.Method != http.MethodPost ||
		strings.TrimRight(req.URL.Path, "/") != "/api/graphql" {
		return resp, err
	}
	const limit = 8 << 20
	body, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	resp.Body.Close()
	if err != nil || len(body) > limit {
		return nil, errors.New("interaction response could not be read")
	}
	resp.Body = io.NopCloser(bytes.NewReader(body))
	if rootRequest {
		t.rootPage = readCommentRoot(body, *t.rootInput, t.cookies)
		return resp, nil
	}
	for _, line := range bytes.Split(body, []byte("\n")) {
		line = bytes.TrimSpace(line)
		line = bytes.TrimPrefix(bytes.TrimPrefix(line, []byte("for(;;);")), []byte("for (;;);"))
		var value struct {
			Data struct {
				Feedback map[string]any `json:"feedback"`
				React    struct {
					Feedback struct {
						ID string `json:"id"`
					} `json:"feedback"`
				} `json:"feedback_react"`
			} `json:"data"`
		}
		decoder := json.NewDecoder(bytes.NewReader(line))
		decoder.UseNumber()
		if decoder.Decode(&value) != nil {
			continue
		}
		if fb := value.Data.Feedback; fb["id"] == nil || fb["id"] == t.feedbackID {
			comments, _ := fb["comments"].(map[string]any)
			_, edges := comments["edges"].([]any)
			_, pageInfo := comments["page_info"].(map[string]any)
			t.commentPage = t.commentPage || (edges && pageInfo)
			commentCount, reactions := feedbackCounts(fb)
			if commentCount != nil {
				t.commentCount = commentCount
			}
			if reactions != nil {
				t.reactionCount = reactions
			}
		}
		t.reactionSaved = t.reactionSaved || value.Data.React.Feedback.ID == t.feedbackID
	}
	return resp, nil
}

func feedbackCount(feedback map[string]any, paths ...[]string) *int64 {
	for _, path := range paths {
		var value any = feedback
		for _, key := range path {
			node, _ := value.(map[string]any)
			value = node[key]
		}
		if n, err := json.Number(scalarString(value)).Int64(); err == nil && n >= 0 && n <= 2147483647 {
			return &n
		}
	}
	return nil
}

func feedbackCounts(feedback map[string]any) (comments, reactions *int64) {
	return feedbackCount(feedback, []string{"comments_count_summary_renderer", "feedback", "comment_rendering_instance", "comments", "total_count"},
			[]string{"comment_rendering_instance", "comments", "total_count"}, []string{"comment_count", "total_count"},
			[]string{"total_comment_count"}, []string{"comments", "total_count"}),
		feedbackCount(feedback, []string{"reaction_count", "count"}, []string{"reactors", "count"}, []string{"i18n_reaction_count"})
}

func storyFeedbackCounts(story map[string]any, feedbackID string) (comments, reactions *int64) {
	// Only follow this post's own feedback sections.
	paths := [][]string{{"feedback"}, {"comet_sections", "feedback", "story", "feedback"},
		{"comet_sections", "feedback", "story", "comet_sections", "feedback", "story", "feedback"},
		{"comet_sections", "context_layout", "story", "feedback"}}
	var target map[string]any = story
	for _, key := range []string{"comet_sections", "feedback", "story", "story_ufi_container", "story", "feedback_context", "feedback_target_with_context"} {
		target, _ = target[key].(map[string]any)
	}
	summary, _ := target["comet_ufi_summary_and_actions_renderer"].(map[string]any)
	ownFeedback, _ := summary["feedback"].(map[string]any)
	if feedbackID != "" && ownFeedback["id"] == feedbackID {
		comments, reactions = feedbackCounts(target)
		renderers, _ := ownFeedback["adaptive_ufi_action_renderers"].([]any)
		for _, value := range renderers {
			renderer, _ := value.(map[string]any)
			feedback, _ := renderer["feedback"].(map[string]any)
			if feedback["id"] != feedbackID {
				continue
			}
			cc, rc := feedbackCounts(feedback)
			if comments == nil {
				comments = cc
			}
			if reactions == nil {
				reactions = rc
			}
		}
	}
	for _, path := range paths {
		feedback := story
		for _, key := range path {
			feedback, _ = feedback[key].(map[string]any)
		}
		if feedback == nil || (feedback["id"] != nil && feedback["id"] != feedbackID) {
			continue
		}
		cc, rc := feedbackCounts(feedback)
		if comments == nil {
			comments = cc
		}
		if reactions == nil {
			reactions = rc
		}
	}
	return comments, reactions
}

func runEngagement(input io.Reader, output io.Writer, getenv func(string) string) int {
	response := fetchEngagement(input, getenv)
	if json.NewEncoder(output).Encode(response) != nil {
		return 1
	}
	return 0
}

func fetchEngagement(input io.Reader, getenv func(string) string) engagementOutput {
	var data engagementInput
	decoder := json.NewDecoder(io.LimitReader(input, 32<<10))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&data) != nil || decoder.Decode(new(any)) != io.EOF ||
		!engagementID.MatchString(data.FeedbackID) || !numericID.MatchString(data.Account) ||
		len(data.Cursor) > 4096 || strings.ContainsAny(data.Cursor, "\x00\r\n") {
		return engagementOutput{Error: "invalid_input"}
	}
	switch data.Operation {
	case "comments":
	case "comment":
		data.Message = strings.TrimSpace(data.Message)
		if data.Message == "" || utf8.RuneCountInString(data.Message) > 2000 || strings.ContainsRune(data.Message, '\x00') {
			return engagementOutput{Error: "invalid_input"}
		}
	case "reaction":
		switch groups.ReactionType(data.Reaction) {
		case groups.ReactionLike, groups.ReactionLove, groups.ReactionCare, groups.ReactionHaha,
			groups.ReactionWow, groups.ReactionSad, groups.ReactionAngry:
		default:
			return engagementOutput{Error: "invalid_input"}
		}
	default:
		return engagementOutput{Error: "invalid_input"}
	}
	cookies, err := parseCookies(getenv("FACEBOOK_COOKIE"), data.Account)
	if err != nil || (getenv("FACEBOOK_USER_ID") != "" && getenv("FACEBOOK_USER_ID") != data.Account) {
		return engagementOutput{Error: "authentication_failed"}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 13*time.Second)
	defer cancel()
	transport := &engagementTransport{base: http.DefaultTransport, feedbackID: data.FeedbackID, cookies: cookies}
	if data.Operation == "comments" && data.Cursor == "" {
		transport.rootInput = &data
	}
	client, err := groups.NewContext(ctx, cookies, groups.WithRetry(1, 0),
		groups.WithHTTPClient(&http.Client{Transport: transport}))
	if err != nil {
		return engagementOutput{Error: classify(err, "bootstrap", 0).Code}
	}
	if client.VerifiedUserID() != data.Account {
		return engagementOutput{Error: "authentication_failed"}
	}
	result := engagementOutput{Account: data.Account}
	switch data.Operation {
	case "comments":
		var page groups.CommentPage
		page, err = client.GetPostCommentsPage(ctx, data.FeedbackID, data.Cursor)
		if err == nil && transport.rootPage != nil {
			if transport.rootPage.ReactionCount == nil {
				stats, postErr := transport.readPost(ctx, data, cookies)
				switch code := classify(postErr, "post", 0).Code; code {
				case "rate_limited", "checkpoint_required", "session_expired", "account_restricted", "authentication_failed":
					return engagementOutput{Error: code}
				}
				if stats != nil {
					transport.rootPage.ReactionCount = stats.ReactionCount
				}
			}
			return *transport.rootPage
		}
		if data.Cursor == "" && (err != nil || (!transport.commentPage && len(page.Comments) == 0)) {
			code := classify(err, "engagement", 0).Code
			if code == "network_error" || code == "parse_error" || code == "unknown_error" {
				preview, postErr := transport.readPost(ctx, data, cookies)
				if postErr != nil {
					return engagementOutput{Error: classify(postErr, "post", 0).Code}
				}
				if preview != nil {
					return *preview
				}
			}
		}
		if err == nil {
			if (!transport.commentPage && len(page.Comments) == 0) ||
				(page.HasNext && (page.NextCursor == "" || page.NextCursor == data.Cursor || len(page.NextCursor) > 4096 ||
					strings.ContainsAny(page.NextCursor, "\x00\r\n") || containsMediaSecret(page.NextCursor, cookieSecrets(cookies)))) {
				return engagementOutput{Error: "parse_error"}
			}
			comments := make([]interactionComment, 0, len(page.Comments))
			for _, comment := range page.Comments {
				if safe := cleanComment(comment, cookies); safe != nil {
					comments = append(comments, interactionComment{Comment: *safe})
				}
			}
			if len(comments) == 0 && len(page.Comments) != 0 {
				return engagementOutput{Error: "parse_error"}
			}
			result.Comments = &comments
			if page.HasNext {
				result.NextCursor = page.NextCursor
			}
			result.CommentCount, result.ReactionCount = transport.commentCount, transport.reactionCount
		}
	case "comment":
		var comment *groups.Comment
		comment, err = client.CreateComment(ctx, data.FeedbackID, data.Message)
		if err == nil && comment != nil {
			result.Comment = cleanComment(*comment, cookies)
		}
		if err == nil && result.Comment == nil {
			err = groups.ErrWriteUnknown
		}
	case "reaction":
		err = client.ReactToPost(ctx, data.FeedbackID, groups.ReactionType(data.Reaction))
		if err == nil && !transport.reactionSaved {
			err = groups.ErrWriteUnknown
		}
		if err == nil {
			result.Reaction = data.Reaction
		}
	}
	if errors.Is(err, groups.ErrWriteUnknown) {
		return engagementOutput{Error: "write_unknown"}
	}
	if err != nil {
		return engagementOutput{Error: classify(err, "engagement", 0).Code}
	}
	return result
}

func cookieSecrets(cookies groups.Cookies) []string {
	var secrets []string
	for key, value := range cookies.Jar {
		if key != "c_user" && key != "ps_l" && key != "ps_n" && (len(value) >= 4 || key == "xs") {
			secrets = append(secrets, value)
		}
	}
	return secrets
}

func cleanComment(comment groups.Comment, cookies groups.Cookies) *groups.Comment {
	secrets := cookieSecrets(cookies)
	if !engagementID.MatchString(comment.ID) || containsMediaSecret(comment.ID, secrets) {
		return nil
	}
	comment.FeedbackID = ""
	if !numericID.MatchString(comment.AuthorID) || containsMediaSecret(comment.AuthorID, secrets) {
		comment.AuthorID = ""
	}
	comment.AuthorName = cleanString(comment.AuthorName, secrets)
	comment.Message = cleanString(comment.Message, secrets)
	return &comment
}
