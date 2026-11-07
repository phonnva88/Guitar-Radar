package main

import (
	"bytes"
	"context"
	"encoding/json"
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

func decodeStory(t *testing.T, s string) map[string]any {
	t.Helper()
	var m map[string]any
	dec := json.NewDecoder(strings.NewReader(s))
	dec.UseNumber()
	if err := dec.Decode(&m); err != nil {
		t.Fatal(err)
	}
	return m
}

func TestImageLayoutsSkipProfilesLinksVideosAndSharedStories(t *testing.T) {
	story := decodeStory(t, `{"post_id":"999","__typename":"Story","actors":[{"__typename":"User","profile_picture":{"__typename":"Photo","url":"https://cdn.example/avatar.jpg"}}],"attached_story":{"attachments":[{"media":{"__typename":"Photo","image":{"uri":"https://cdn.example/shared.jpg"}}}]},"comet_sections":{"content":{"story":{"attachments":[{"media":{"__typename":"Photo","url":"https://www.facebook.com/photo/?fbid=1","image":{"uri":"https://cdn.example/picture-no-extension?size=1080","width":1080,"height":1440}}},{"subattachments":{"nodes":[{"media":{"__typename":"Photo","viewer_image":{"uri":"https://cdn.example/second.jpg"}}}]}},{"media":{"__typename":"Video","image":{"uri":"https://cdn.example/video-thumb.jpg"}}},{"style_list":["external"],"media":{"__typename":"Photo","image":{"uri":"https://cdn.example/link-preview.jpg"}}}]}}}}`)
	images := extractImages(story)
	if len(images) != 2 || images[0].URL != "https://cdn.example/picture-no-extension?size=1080" || images[0].Width != int64(1080) || images[1].URL != "https://cdn.example/second.jpg" {
		t.Fatalf("media candidates: %+v", images)
	}
	if images[0].Source != "attachment" || !strings.Contains(images[0].Path, "comet_sections.content.story.attachments") {
		t.Fatal(images)
	}
}

func TestCaptureSanitizesDebugAndKeepsOnlyFeedStories(t *testing.T) {
	dir := t.TempDir()
	capture := &rawCapture{Stories: map[string]*storyEvidence{}, Secrets: []string{"REAL_SESSION_SECRET"}, DebugDir: dir}
	body := `{"data":{"group":{"group_feed":{"edges":[{"node":{"__typename":"Story","post_id":"123","id":"story-123","message":{"text":"cookie: xs=REAL_SESSION_SECRET"},"access_token":"REMOVE","xs":"REMOVE","extensions":{"internal_token":"REMOVE"},"attachments":[{"media":{"__typename":"Photo","image":{"uri":"https://cdn.example/photo.jpg?access_token=REAL_SESSION_SECRET&size=100"}}}]}}]}},"recommendation":{"__typename":"Story","post_id":"888","attachments":[{"media":{"__typename":"Photo","image":{"uri":"https://cdn.example/recommended.jpg"}}}]}}}`
	capture.consume([]byte(body))
	record := capture.Stories["123"]
	if record == nil || len(capture.Stories) != 2 || record.DebugFile == "" || capture.Stories["888"] != nil {
		t.Fatalf("capture: %+v", capture)
	}
	data, err := os.ReadFile(record.DebugFile)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), "REAL_SESSION_SECRET") || strings.Contains(string(data), "REMOVE") || strings.Contains(record.Images[0].URL, "access_token") {
		t.Fatalf("credential persisted: %s", data)
	}
	if info, _ := os.Stat(record.DebugFile); info.Mode().Perm() != 0600 {
		t.Fatal(info.Mode())
	}
	first := append([]byte(nil), data...)
	capture.consume([]byte(strings.ReplaceAll(body, "size=100", "size=200")))
	data, _ = os.ReadFile(record.DebugFile)
	if !bytes.Equal(first, data) {
		t.Fatal("first response overwritten")
	}
}

func TestCaptureTransportNeverCapturesBootstrapAndPreservesBodies(t *testing.T) {
	capture := &rawCapture{Stories: map[string]*storyEvidence{}}
	const payload = `{"data":{"group":{"group_feed":{"edges":[]}}}}`
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(payload)), Header: http.Header{}, Request: req}, nil
	})
	transport := &captureTransport{Base: base, Capture: capture}
	for _, method := range []string{"GET", "POST"} {
		req, _ := http.NewRequest(method, "https://www.facebook.com/api/graphql/", nil)
		resp, err := transport.RoundTrip(req)
		if err != nil {
			t.Fatal(err)
		}
		b, _ := io.ReadAll(resp.Body)
		if string(b) != payload {
			t.Fatal("body changed")
		}
	}
}

func datedPost(id, when string) groups.Post {
	ts, _ := time.Parse(time.RFC3339, when)
	return groups.Post{ID: id, CreatedAt: ts}
}

func TestWindowKeepsRawOutsideBoundsAndContinuesPastPinnedOlderPost(t *testing.T) {
	f := &fakeClient{pages: []groups.FeedPage{
		{Posts: []groups.Post{datedPost("pinned", "2026-11-07T01:00:00Z"), datedPost("recent", "2026-11-07T12:00:00Z")}, HasNext: true, NextCursor: "second"},
		{Posts: []groups.Post{datedPost("older", "2026-11-07T00:00:00Z")}, HasNext: true, NextCursor: "third"},
		{Posts: []groups.Post{datedPost("newer-after-old", "2026-11-07T10:00:00Z")}},
	}}
	code, result, _ := invoke(t, f, nil, "--pages", "100", "--from", "2026-11-07T08:00:00Z", "--to", "2026-11-07T11:00:00Z")
	if code != 0 || len(f.calls) != 3 || len(result.Posts) != 4 || result.Window.StopReason != "feed_exhausted" || result.Window.Complete || result.Window.OrderingViolations != 2 {
		t.Fatalf("window: %+v calls %d", result.Window, len(f.calls))
	}
}

func TestWindowCompletionAndSafetyCap(t *testing.T) {
	for _, tc := range []struct {
		name     string
		pages    int
		feed     []groups.FeedPage
		complete bool
		reason   string
	}{
		{"boundary", 2, []groups.FeedPage{{Posts: []groups.Post{datedPost("recent", "2026-11-07T10:00:00Z")}, HasNext: true, NextCursor: "second"}, {Posts: []groups.Post{datedPost("old", "2026-11-07T07:00:00Z")}, HasNext: true, NextCursor: "third"}}, true, "older_than_from"},
		{"cap", 1, []groups.FeedPage{{Posts: []groups.Post{datedPost("recent", "2026-11-07T10:00:00Z")}, HasNext: true, NextCursor: "second"}}, false, "page_limit"},
		{"unknown", 1, []groups.FeedPage{{Posts: []groups.Post{{ID: "unknown"}}}}, false, "feed_exhausted"},
		{"exhausted", 1, []groups.FeedPage{{Posts: []groups.Post{datedPost("recent", "2026-11-07T10:00:00Z")}}}, true, "feed_exhausted"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeClient{pages: tc.feed}
			_, result, _ := invoke(t, f, nil, "--pages", fmt.Sprint(tc.pages), "--from", "2026-11-07T08:00:00Z", "--to", "2026-11-07T11:00:00Z")
			if result.Window.Complete != tc.complete || result.Window.StopReason != tc.reason || result.Window.ScannedPosts != len(result.Posts) {
				t.Fatalf("window: %+v", result.Window)
			}
		})
	}
}

func TestWindowInvalidArgumentsFailBeforeRequest(t *testing.T) {
	for _, args := range [][]string{{"--from", "2026-11-07T08:00:00Z"}, {"--from", "2026-11-07T08:00:00", "--to", "2026-11-07T11:00:00Z"}, {"--from", "2026-11-07T12:00:00Z", "--to", "2026-11-07T11:00:00Z"}} {
		f := &fakeClient{}
		code, result, _ := invoke(t, f, nil, args...)
		if code == 0 || len(f.calls) > 0 || result.Errors[0].Stage != "configuration" {
			t.Fatal(result)
		}
	}
}

func TestActualLibraryMediaCaptureAndInvalidCursor(t *testing.T) {
	transport := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		body := `"CurrentUserInitialData",[],{"USER_ID":"123","NAME":"Fixture"} "DTSGInitialData",[],{"token":"fixture"} "LSD",[],{"token":"fixture"} "client_revision":1,"haste_session":"fixture","hsi":"1","__spin_t":1`
		if req.Method == http.MethodPost {
			body = `{"data":{"group":{"id":"999","group_feed":{"edges":[{"node":{"__typename":"Story","id":"story","post_id":"10","message":{"text":"Fixture"},"creation_time":1794000000,"attachments":[{"media":{"__typename":"Photo","image":{"uri":"https://cdn.example/no-extension","width":42,"height":24}}}]}}],"page_info":{"has_next_page":true,"end_cursor":null}}}}}`
		}
		return &http.Response{StatusCode: 200, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(body)), Request: req}, nil
	})
	factory := func(ctx context.Context, c groups.Cookies) (groupClient, error) {
		capture := ctx.Value(captureKey{}).(*rawCapture)
		return groups.NewContext(ctx, c, groups.WithHTTPClient(&http.Client{Transport: &captureTransport{Base: transport, Capture: capture}}), groups.WithMinRequestGap(0), groups.WithRetry(1, 0))
	}
	dir := t.TempDir()
	out := filepath.Join(dir, "raw.json")
	code := run([]string{"--output", out, "--debug-dir", filepath.Join(dir, "debug"), "--from", "2026-11-07T00:00:00Z", "--to", "2026-11-07T23:00:00Z"}, testEnv, io.Discard, factory)
	data, _ := os.ReadFile(out)
	var result envelope
	json.Unmarshal(data, &result)
	if code != 1 || len(result.Posts) != 1 || result.Errors[0].Code != "invalid_cursor" || result.Window.Complete {
		t.Fatalf("result: %s", data)
	}
	var post map[string]any
	json.Unmarshal(result.Posts[0], &post)
	if len(post["image_candidates"].([]any)) != 1 || post["debug_response_saved"] != true {
		t.Fatal(post)
	}
}
