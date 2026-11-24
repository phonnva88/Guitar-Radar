package main

import (
	"bytes"
	"encoding/json"
	"testing"

	"github.com/teslashibe/facebook-go/groups"
)

func TestEngagementCountsRequireExplicitTotalsForOwnPost(t *testing.T) {
	story := decodeStory(t, `{
		"feedback":{"id":"feedback-1"},
		"attached_story":{"feedback":{"id":"foreign","total_comment_count":30}},
		"comet_sections":{"feedback":{"story":{"story_ufi_container":{"story":{
			"feedback_context":{"feedback_target_with_context":{
				"comment_rendering_instance":{"comments":{"total_count":0}},
				"comet_ufi_summary_and_actions_renderer":{"feedback":{
					"id":"feedback-1",
					"top_reactions":{"edges":[{"reaction_count":1}]},
					"adaptive_ufi_action_renderers":[{"feedback":{"id":"feedback-1","reaction_count":{"count":12}}}]
				}}
			}}
		}}}}}
	}`)
	comments, reactions := storyFeedbackCounts(story, "feedback-1")
	if comments == nil || *comments != 0 || reactions == nil || *reactions != 12 {
		t.Fatalf("explicit totals were not collected: %v %v", comments, reactions)
	}
	comments, reactions = storyFeedbackCounts(story, "foreign")
	if comments != nil || reactions != nil {
		t.Fatalf("picked up another post's counts")
	}
}

func TestPostPreviewUsesOnlyMatchingFeedback(t *testing.T) {
	html := []byte(`<script type="application/json">{
		"data":{"node":{"post_id":"101","feedback":{"id":"feedback:101","reaction_count":{"count":3},
		  "comments":{"total_count":5,"edges":[{"node":{"__typename":"Comment","id":"comment:1",
		    "author":{"name":"Test author"},"body":{"text":"Hello"}}}]} }},
		  "suggested":{"feedback":{"id":"feedback:100","reaction_count":{"count":87},
		    "comments":{"edges":[{"node":{"__typename":"Comment","id":"other","body":{"text":"Other post"}}}]}}}}
	}</script>`)
	preview := postPreview(html, engagementInput{PostID: "101", FeedbackID: "feedback:101", Account: "100"}, groups.Cookies{})
	if !preview.PreviewOnly || preview.Comments == nil || len(*preview.Comments) != 1 || (*preview.Comments)[0].ID != "comment:1" ||
		preview.CommentCount == nil || *preview.CommentCount != 5 || preview.ReactionCount == nil || *preview.ReactionCount != 3 {
		t.Fatalf("incorrect preview: %+v", preview)
	}
}

func TestReadCommentRootIncludesRepliesAndReportsMissingRows(t *testing.T) {
	body := []byte(`{"data":{"node":{"__typename":"Feedback","id":"feedback:101",
		"comment_rendering_instance_for_feed_location":{"comments":{"total_count":3,
			"page_info":{"has_next_page":true},"edges":[{"node":{
				"__typename":"Comment","id":"comment:1","body":{"text":"Question"},
				"feedback":{"replies_connection":{"page_info":{"has_next_page":false},"edges":[{"node":{
					"__typename":"Comment","id":"comment:2","body":{"text":"Answer"}
				}}]}}
			}}]
		}}
	}}}`)
	var packet bytes.Buffer
	if err := json.Compact(&packet, body); err != nil {
		t.Fatal(err)
	}
	page := readCommentRoot(packet.Bytes(), engagementInput{FeedbackID: "feedback:101", Account: "100"}, groups.Cookies{})
	if page == nil || page.Comments == nil || len(*page.Comments) != 2 || !page.PreviewOnly ||
		(*page.Comments)[1].ParentID != "comment:1" || page.CommentCount == nil || *page.CommentCount != 3 {
		t.Fatalf("incorrect root comment response: %+v", page)
	}
}
