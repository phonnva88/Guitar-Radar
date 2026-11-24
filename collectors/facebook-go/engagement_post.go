package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/teslashibe/facebook-go/groups"
)

func (t *engagementTransport) readPost(ctx context.Context, input engagementInput, cookies groups.Cookies) (*engagementOutput, error) {
	if t.headers == nil || !numericID.MatchString(input.GroupID) || !numericID.MatchString(input.PostID) {
		return nil, nil
	}
	target := "https://www.facebook.com/groups/" + input.GroupID + "/posts/" + input.PostID + "/"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return nil, err
	}
	req.Header = t.headers.Clone()
	resp, err := t.base.RoundTrip(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusTooManyRequests || resp.Header.Get("Retry-After") != "" {
		return nil, groups.ErrRateLimited
	}
	if resp.StatusCode >= 300 && resp.StatusCode < 400 {
		switch {
		case strings.Contains(resp.Header.Get("Location"), "/checkpoint"):
			return nil, groups.ErrCheckpoint
		case strings.Contains(resp.Header.Get("Location"), "/login"):
			return nil, groups.ErrSessionExpired
		}
	}
	if resp.StatusCode != http.StatusOK {
		return nil, nil
	}
	const limit = 10 << 20
	html, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil || len(html) > limit {
		return nil, nil
	}
	return postPreview(html, input, cookies), nil
}

func postPreview(html []byte, input engagementInput, cookies groups.Cookies) *engagementOutput {
	preview := engagementOutput{Account: input.Account}
	var comments []interactionComment
	var visit func(any, int)
	visit = func(value any, depth int) {
		if depth > 48 {
			return
		}
		switch node := value.(type) {
		case map[string]any:
			if scalarString(node["post_id"]) == input.PostID {
				cc, rc := storyFeedbackCounts(node, input.FeedbackID)
				if cc != nil {
					preview.CommentCount = cc
				}
				if rc != nil {
					preview.ReactionCount = rc
				}
			}
			if node["id"] == input.FeedbackID {
				cc, rc := feedbackCounts(node)
				if cc != nil {
					preview.CommentCount = cc
				}
				if rc != nil {
					preview.ReactionCount = rc
				}
				rendering, _ := node["comment_rendering_instance"].(map[string]any)
				for _, branch := range []map[string]any{node, rendering} {
					for _, key := range []string{"comments", "display_comments"} {
						connection, _ := branch[key].(map[string]any)
						if page, _ := embeddedComments(connection, cookies); len(page) > len(comments) {
							comments = page
						}
					}
				}
			}
			for key, child := range node {
				if key != "attachments" && key != "profile_picture" && key != "actors" {
					visit(child, depth+1)
				}
			}
		case []any:
			for _, child := range node {
				visit(child, depth+1)
			}
		}
	}
	for _, script := range albumScripts.FindAllSubmatch(html, -1) {
		if !albumJSONType.Match(script[1]) || !bytes.Contains(script[2], []byte(input.FeedbackID)) {
			continue
		}
		var value any
		decoder := json.NewDecoder(bytes.NewReader(script[2]))
		decoder.UseNumber()
		if decoder.Decode(&value) == nil {
			visit(value, 0)
		}
	}
	if len(comments) != 0 {
		preview.Comments = &comments
		preview.PreviewOnly = true
	} else if preview.CommentCount != nil && *preview.CommentCount == 0 {
		empty := []interactionComment{}
		preview.Comments = &empty
	} else {
		preview.Unavailable = true
	}
	return &preview
}

func embeddedComments(connection map[string]any, cookies groups.Cookies) ([]interactionComment, bool) {
	comments := []interactionComment{}
	seen := map[string]bool{}
	partial := false
	var walk func(map[string]any, string, int)
	walk = func(branch map[string]any, parent string, depth int) {
		if branch == nil {
			return
		}
		page, _ := branch["page_info"].(map[string]any)
		items, _ := branch["edges"].([]any)
		if page["has_next_page"] != false && len(items) > 0 {
			partial = true
		}
		if depth > 8 || len(comments) >= 100 {
			partial = partial || len(items) != 0
			return
		}
		for _, value := range items {
			if len(comments) >= 100 {
				partial = true
				break
			}
			edge, _ := value.(map[string]any)
			node, _ := edge["node"].(map[string]any)
			if node["__typename"] != "Comment" {
				partial = true
				continue
			}
			id := scalarString(node["id"])
			if seen[id] {
				continue
			}
			author, _ := node["author"].(map[string]any)
			body, _ := node["body"].(map[string]any)
			created := time.Time{}
			if unix := feedbackCount(node, []string{"created_time"}); unix != nil && *unix > 0 {
				created = time.Unix(*unix, 0).UTC()
			}
			comment := groups.Comment{ID: id, AuthorName: scalarString(author["name"]), Message: scalarString(body["text"]), CreatedAt: created}
			if clean := cleanComment(comment, cookies); clean != nil {
				seen[id] = true
				comments = append(comments, interactionComment{Comment: *clean, ParentID: parent})
				feedback, _ := node["feedback"].(map[string]any)
				replies, _ := feedback["replies_connection"].(map[string]any)
				walk(replies, id, depth+1)
			} else {
				partial = true
			}
		}
	}
	walk(connection, "", 0)
	return comments, partial
}
