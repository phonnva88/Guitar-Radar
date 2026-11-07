package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/teslashibe/facebook-go/groups"
)

type fakeClient struct {
	calls   []groups.PostListOptions
	pages   []groups.FeedPage
	err     error
	panicAt int
}

func (f *fakeClient) ListGroupPosts(_ context.Context, user, group string, opts groups.PostListOptions) (groups.FeedPage, error) {
	f.calls = append(f.calls, opts)
	if f.panicAt == len(f.calls) {
		panic("sensitive upstream parser details")
	}
	if len(f.calls) > len(f.pages) {
		return groups.FeedPage{}, f.err
	}
	return f.pages[len(f.calls)-1], nil
}

func testEnv(name string) string {
	return map[string]string{"FACEBOOK_COOKIE": "c_user=123; xs=secret-that-must-not-leak; datr=opaque", "FACEBOOK_GROUP_ID": "999"}[name]
}

func invoke(t *testing.T, client groupClient, bootErr error, extra ...string) (int, envelope, string) {
	t.Helper()
	out := filepath.Join(t.TempDir(), "raw", "result.json")
	var logs bytes.Buffer
	args := append([]string{"--output", out, "--run-id", "test-run"}, extra...)
	code := run(args, testEnv, &logs, func(context.Context, groups.Cookies) (groupClient, error) { return client, bootErr })
	data, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data)+logs.String(), "secret-that-must-not-leak") {
		t.Fatal("credential appeared in output")
	}
	var result envelope
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(out)
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatalf("output mode: %v %v", info, err)
	}
	return code, result, logs.String()
}

func TestPaginationAndChronologicalSingleGroup(t *testing.T) {
	f := &fakeClient{pages: []groups.FeedPage{
		{Posts: []groups.Post{{ID: "10"}}, HasNext: true, NextCursor: "page-two"},
		{Posts: []groups.Post{{ID: "11"}}},
	}}
	code, result, _ := invoke(t, f, nil, "--group", "https://www.facebook.com/groups/999/?ref=test", "--pages", "2", "--page-size", "10", "--timeout", "4.5")
	if code != 0 || result.Status != "success" || result.PagesFetched != 2 || len(result.Posts) != 2 {
		t.Fatalf("unexpected result: %+v", result)
	}
	if f.calls[0].Cursor != "" || f.calls[1].Cursor != "page-two" || f.calls[0].Sort != groups.SortChronological || f.calls[0].Count != 10 {
		t.Fatalf("calls: %+v", f.calls)
	}
	if result.Source != "live" || result.GroupID != "999" || result.RunID != "test-run" || result.RequestedPages != 2 || result.PageSize != 10 {
		t.Fatalf("metadata: %+v", result)
	}
}

func TestPartialFailureRetainsPriorPageAndDoesNotLeakError(t *testing.T) {
	f := &fakeClient{pages: []groups.FeedPage{{Posts: []groups.Post{{ID: "10"}}, HasNext: true, NextCursor: "next"}}, err: fmt.Errorf("%w: secret-that-must-not-leak", groups.ErrRateLimited)}
	code, result, logs := invoke(t, f, nil)
	if code != 1 || result.Status != "partial" || len(result.Posts) != 1 || result.Errors[0].Code != "rate_limited" || result.Errors[0].Page != 2 || len(f.calls) != 2 {
		t.Fatalf("result: %+v", result)
	}
	if !strings.Contains(logs, "page=1 posts=1") {
		t.Fatal(logs)
	}
}

func TestRepeatedCursorStops(t *testing.T) {
	f := &fakeClient{pages: []groups.FeedPage{
		{Posts: []groups.Post{{ID: "10"}}, HasNext: true, NextCursor: "same"},
		{Posts: []groups.Post{{ID: "11"}}, HasNext: true, NextCursor: "same"},
	}}
	_, result, _ := invoke(t, f, nil, "--pages", "10")
	if result.Status != "partial" || result.Errors[0].Stage != "pagination" || len(f.calls) != 2 {
		t.Fatalf("result: %+v", result)
	}
}

func TestMalformedPostDoesNotDiscardSiblings(t *testing.T) {
	f := &fakeClient{pages: []groups.FeedPage{{Posts: []groups.Post{
		{ID: "good-first"},
		{ID: "bad", CreatedAt: time.Date(10000, 1, 1, 0, 0, 0, 0, time.UTC)},
		{ID: "good-last"},
	}}}}
	code, result, _ := invoke(t, f, nil)
	if code != 1 || result.Status != "partial" || len(result.Posts) != 2 || result.Errors[0].Code != "parse_error" || result.Errors[0].Post != 2 {
		t.Fatalf("result: %+v", result)
	}
}

func TestParserPanicBecomesErrorAndKeepsPriorPage(t *testing.T) {
	f := &fakeClient{pages: []groups.FeedPage{{Posts: []groups.Post{{ID: "ok"}}, HasNext: true, NextCursor: "next"}}, panicAt: 2}
	code, result, _ := invoke(t, f, nil)
	if code != 1 || len(result.Posts) != 1 || result.Errors[0].Code != "parse_error" {
		t.Fatalf("result: %+v", result)
	}
}

func TestBootstrapCheckpointStops(t *testing.T) {
	code, result, _ := invoke(t, nil, fmt.Errorf("%w: secret-that-must-not-leak", groups.ErrCheckpoint))
	if code != 1 || result.Status != "failed" || result.Errors[0].Code != "checkpoint_required" || result.PagesFetched != 0 {
		t.Fatalf("result: %+v", result)
	}
}

func TestMissingSessionWritesErrorWithoutRequests(t *testing.T) {
	out := filepath.Join(t.TempDir(), "error.json")
	called := false
	code := run([]string{"--group", "999", "--output", out}, func(string) string { return "" }, io.Discard, func(context.Context, groups.Cookies) (groupClient, error) { called = true; return nil, nil })
	data, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	var result envelope
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatal(err)
	}
	if code != 1 || called || result.Errors[0].Code != "authentication_failed" || result.Status != "failed" || len(result.Posts) != 0 {
		t.Fatalf("result: %+v", result)
	}
}

func TestInputValidation(t *testing.T) {
	for _, good := range []string{"999", "https://facebook.com/groups/999/", "https://m.facebook.com/groups/999?ref=x"} {
		if got, err := parseGroup(good); err != nil || got != "999" {
			t.Fatalf("good input rejected: %s %v", good, err)
		}
	}
	for _, bad := range []string{"", "https://evil.example/groups/999", "https://facebook.com.evil.example/groups/999", "https://user:secret@facebook.com/groups/999", "https://facebook.com/groups/vanity", "https://facebook.com/groups/999/posts/123", "file:///groups/999", "https://facebook.com:443/groups/999"} {
		if _, err := parseGroup(bad); err == nil {
			t.Fatalf("accepted invalid group input: %s", bad)
		}
	}
	for _, raw := range []string{"", "xs=a", "c_user=123", "c_user=abc; xs=x", "c_user=123; xs=x\nother=1", "c_user=123; xs=x; xs=y"} {
		if _, err := parseCookies(raw, ""); err == nil {
			t.Fatal("accepted invalid cookie")
		}
	}
	if _, err := parseCookies("c_user=123; xs=a", "456"); err == nil {
		t.Fatal("mismatched user accepted")
	}
}

func TestErrorClassification(t *testing.T) {
	cases := []struct {
		err  error
		code string
	}{
		{groups.ErrInvalidAuth, "authentication_failed"}, {groups.ErrUnauthorized, "authentication_failed"},
		{groups.ErrSessionExpired, "session_expired"}, {groups.ErrCheckpoint, "checkpoint_required"},
		{groups.ErrRateLimited, "rate_limited"}, {groups.ErrFeatureBlocked, "rate_limited"},
		{groups.ErrNotMember, "group_not_accessible"}, {groups.ErrForbidden, "group_not_accessible"},
		{groups.ErrNotFound, "group_not_found"}, {groups.ErrDocIDStale, "parse_error"},
		{context.DeadlineExceeded, "network_error"},
		{fmt.Errorf("%w: decoding feed with secret-that-must-not-leak", groups.ErrRequestFailed), "parse_error"},
		{fmt.Errorf("%w: HTTP 503", groups.ErrRequestFailed), "network_error"},
		{errors.New("secret-that-must-not-leak"), "unknown_error"},
	}
	for _, tc := range cases {
		e := classify(fmt.Errorf("wrapped: %w", tc.err), "fetch", 2)
		if e.Code != tc.code || strings.Contains(e.Message, "secret-that-must-not-leak") {
			t.Fatalf("%v => %+v", tc.err, e)
		}
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// Exercise the actual pinned library, without contacting Facebook. This catches
// API/shape drift that a mock groupClient alone would miss.
func TestPinnedLibraryAgainstOfflineTransport(t *testing.T) {
	pageCalls := 0
	transport := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		body := `"CurrentUserInitialData",[],{"USER_ID":"123","NAME":"Fixture"} ` +
			`"DTSGInitialData",[],{"token":"fixture"} "LSD",[],{"token":"fixture"} ` +
			`"client_revision":1,"haste_session":"fixture","hsi":"1","__spin_t":1`
		if req.Method == http.MethodPost {
			pageCalls++
			if err := req.ParseForm(); err != nil {
				t.Fatal(err)
			}
			var vars map[string]any
			if err := json.Unmarshal([]byte(req.Form.Get("variables")), &vars); err != nil {
				t.Fatal(err)
			}
			if vars["sortingSetting"] != "CHRONOLOGICAL" {
				t.Fatal("wrong feed sort")
			}
			if pageCalls == 2 && vars["cursor"] != "next" {
				t.Fatal("cursor not passed to library")
			}
			pageInfo := `{"has_next_page":true,"end_cursor":"next"}`
			if pageCalls == 2 {
				pageInfo = `{"has_next_page":false,"end_cursor":null}`
			}
			body = fmt.Sprintf(`{"data":{"group":{"id":"999","group_feed":{"edges":[{"node":null},{"node":{"__typename":"Story","id":"story","post_id":"%d","message":{"text":"Fixture post"},"creation_time":1794000000,"actors":[{"id":"1","name":"Fixture author"}]}}],"page_info":%s}}}}`, pageCalls, pageInfo)
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(body)), Request: req}, nil
	})
	factory := func(ctx context.Context, c groups.Cookies) (groupClient, error) {
		return groups.NewContext(ctx, c, groups.WithHTTPClient(&http.Client{Transport: transport}), groups.WithMinRequestGap(0), groups.WithRetry(1, 0))
	}
	out := filepath.Join(t.TempDir(), "live-adapter.json")
	code := run([]string{"--output", out}, testEnv, io.Discard, factory)
	data, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	var result envelope
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatal(err)
	}
	if code != 0 || result.PagesFetched != 2 || len(result.Posts) != 2 || pageCalls != 2 {
		t.Fatalf("result: %s", data)
	}
	var post groups.Post
	if err := json.Unmarshal(result.Posts[0], &post); err != nil {
		t.Fatal(err)
	}
	if post.Message != "Fixture post" || post.AuthorName != "Fixture author" || post.ID != "1" || post.GroupID != "999" {
		t.Fatalf("post: %+v", post)
	}
}
