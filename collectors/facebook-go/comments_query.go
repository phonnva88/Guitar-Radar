package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/teslashibe/facebook-go/groups"
)

// The current web view uses this query when opening a post's comments. Reuse
// the library's signed-in transport, identity check and response error handling.
func commentRootRequest(req *http.Request, data engagementInput) (*http.Request, error) {
	if req.Method != http.MethodPost || req.URL.Scheme != "https" || req.URL.Host != "www.facebook.com" ||
		strings.TrimRight(req.URL.Path, "/") != "/api/graphql" || req.GetBody == nil {
		return req, nil
	}
	body, err := req.GetBody()
	if err != nil {
		return nil, err
	}
	defer body.Close()
	encoded, err := io.ReadAll(io.LimitReader(body, 64<<10))
	if err != nil {
		return nil, err
	}
	form, err := url.ParseQuery(string(encoded))
	if err != nil {
		return nil, err
	}
	if form.Get("fb_api_req_friendly_name") != "CommentsListComponentPaginationQuery" {
		return req, nil
	}
	variables := map[string]any{
		"id": data.FeedbackID, "commentsIntentToken": nil, "feedLocation": "POST_PERMALINK_DIALOG",
		"feedbackSource": 2, "focusCommentID": nil, "scale": 1, "useDefaultActor": false,
		"__relay_internal__pv__CometUFICommentAutoTranslationTyperelayprovider":        "ORIGINAL",
		"__relay_internal__pv__IsWorkUserrelayprovider":                                false,
		"__relay_internal__pv__CometUFICommentAvatarStickerAnimatedImagerelayprovider": false,
		"__relay_internal__pv__CometUFICommentActionLinksRewriteEnabledrelayprovider":  true,
		"__relay_internal__pv__CometUFICommentActionLinkStableIDsrelayprovider":        false,
	}
	raw, err := json.Marshal(variables)
	if err != nil {
		return nil, err
	}
	const name = "CommentListComponentsRootQuery"
	form.Set("variables", string(raw))
	form.Set("doc_id", "29103885182587417")
	form.Set("fb_api_req_friendly_name", name)
	payload := form.Encode()
	out := req.Clone(req.Context())
	out.Header.Set("x-fb-friendly-name", name)
	out.ContentLength = int64(len(payload))
	out.GetBody = func() (io.ReadCloser, error) { return io.NopCloser(strings.NewReader(payload)), nil }
	out.Body, _ = out.GetBody()
	return out, nil
}

func readCommentRoot(body []byte, input engagementInput, cookies groups.Cookies) *engagementOutput {
	for _, line := range bytes.Split(body, []byte("\n")) {
		line = bytes.TrimSpace(line)
		line = bytes.TrimPrefix(bytes.TrimPrefix(line, []byte("for(;;);")), []byte("for (;;);"))
		var packet struct {
			Data struct {
				Node map[string]any `json:"node"`
			} `json:"data"`
		}
		decoder := json.NewDecoder(bytes.NewReader(line))
		decoder.UseNumber()
		if decoder.Decode(&packet) != nil {
			continue
		}
		node := packet.Data.Node
		if node["__typename"] != "Feedback" || node["id"] != input.FeedbackID {
			continue
		}
		instance, _ := node["comment_rendering_instance_for_feed_location"].(map[string]any)
		connection, _ := instance["comments"].(map[string]any)
		edges, valid := connection["edges"].([]any)
		if !valid {
			continue
		}
		items, partial := embeddedComments(connection, cookies)
		if len(edges) > 0 && len(items) == 0 {
			continue
		}
		cc, rc := feedbackCounts(node)
		if cc == nil {
			cc = feedbackCount(connection, []string{"total_count"})
		}
		if cc != nil && int64(len(items)) < *cc {
			partial = true
		}
		return &engagementOutput{Account: input.Account, Comments: &items, PreviewOnly: partial, CommentCount: cc, ReactionCount: rc}
	}
	return nil
}
