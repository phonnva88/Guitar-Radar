package main

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func TestSyntheticVideoStories(t *testing.T) {
	data, err := os.ReadFile("testdata/video_stories.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Cases []struct {
			Name       string         `json:"name"`
			Story      map[string]any `json:"story"`
			Kind       string         `json:"kind"`
			Type       string         `json:"type"`
			VideoCount int            `json:"video_count"`
			ImageCount int            `json:"image_count"`
		} `json:"cases"`
	}
	decoder := json.NewDecoder(strings.NewReader(string(data)))
	decoder.UseNumber()
	if err := decoder.Decode(&fixture); err != nil {
		t.Fatal(err)
	}
	if len(fixture.Cases) < 9 {
		t.Fatal("composite fixture missing expected story layouts")
	}
	for _, tc := range fixture.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			capture := &rawCapture{Stories: map[string]*storyEvidence{}}
			capture.add(tc.Story)
			id := scalarString(tc.Story["post_id"])
			record := capture.Stories[id]
			if record == nil {
				t.Fatal("story was not captured")
			}
			if len(record.Videos) != tc.VideoCount || len(record.Images) != tc.ImageCount || record.PossibleType != tc.Type {
				t.Fatalf("videos=%+v images=%+v type=%q", record.Videos, record.Images, record.PossibleType)
			}
			if tc.VideoCount > 0 && record.Videos[0].Kind != tc.Kind {
				t.Fatalf("video kind=%q, want %q", record.Videos[0].Kind, tc.Kind)
			}
			if tc.Name == "direct_video" {
				video := record.Videos[0]
				if video.VideoID != "video-2001" || video.ThumbnailURL != "https://scontent.xx.fbcdn.net/direct-thumb.jpg" ||
					video.PlaybackURL != "https://video.xx.fbcdn.net/direct.mp4" || video.PlaybackURLExpiresAt != "2026-11-16T12:00:00Z" ||
					video.DurationSeconds == nil || *video.DurationSeconds != 93.5 || video.Width != int64(1920) || video.Height != int64(1080) || !video.PlaybackURLEphemeral {
					t.Fatalf("video metadata: %+v", video)
				}
			}
			if tc.Name == "reel_without_playback" || tc.Name == "video_thumbnail_only" || tc.Name == "shared_video" {
				video := record.Videos[0]
				if video.PlaybackURL != "" || video.PlaybackURLEphemeral {
					t.Fatalf("invented playback URL: %+v", video)
				}
			}
		})
	}
}

func TestVideoURLsAreExplicitAndSafe(t *testing.T) {
	story := decodeStory(t, `{"post_id":"20","attachments":[
		{"media":{"__typename":"Video","id":"v1","playable_url":"https://evil.example/video.mp4","image":{"uri":"https://scontent.xx.fbcdn.net/thumb.jpg","width":100,"height":50}}},
		{"media":{"__typename":"Video","id":"v2","playable_url":"https://video.xx.fbcdn.net/with-session.mp4?access_token=secret"}},
		{"media":{"__typename":"Video","id":"v3","playable_url_dash":"https://video.xx.fbcdn.net/manifest.mpd"}}
	]}`)
	videos := extractVideos(story)
	if len(videos) != 3 {
		t.Fatal(videos)
	}
	for _, video := range videos {
		if video.PlaybackURL != "" || video.Width != nil || video.Height != nil {
			t.Fatalf("unsafe or inferred video metadata: %+v", video)
		}
	}
	if videos[0].ThumbnailURL != "https://scontent.xx.fbcdn.net/thumb.jpg" {
		t.Fatal(videos[0])
	}
}

func TestSharedStoryVideoWithShareStyle(t *testing.T) {
	story := decodeStory(t, `{"post_id":"21","style_list":["share"],"attached_story":{"attachments":[{"media":{"__typename":"Video","id":"v21"}}]}}`)
	videos := extractVideos(story)
	if len(videos) != 1 || videos[0].Kind != "shared" {
		t.Fatalf("shared video lost: %+v", videos)
	}
}

func TestReelWrapperAndSignedPlayback(t *testing.T) {
	const signed = "https://video.xx.fbcdn.net/reel.mp4?oe=F00&oh=fixture%2f%2B&_nc_test=1"
	story := decodeStory(t, `{"post_id":"23","attachments":[
		{"image":{"uri":"https://scontent.xx.fbcdn.net/sibling-poster.jpg"},"media":{
			"__typename":"Reel","video":{"__typename":"Video","id":"v23","playable_url":"`+signed+`"}}},
		{"target":{"__typename":"ExternalUrl"},"media":{"__typename":"Video","id":"outside"}},
		{"media":{"__typename":"Photo","id":"p23","image":{"uri":"https://scontent.xx.fbcdn.net/photo.jpg"}}}
	]}`)
	capture := &rawCapture{Stories: map[string]*storyEvidence{}, DebugDir: t.TempDir()}
	capture.add(story)
	record := capture.Stories["23"]
	if len(record.Videos) != 1 || record.Videos[0].VideoID != "v23" || record.Videos[0].Kind != "reel" ||
		record.Videos[0].PlaybackURL != signed || len(record.Images) != 1 {
		t.Fatalf("unexpected capture: %+v", record)
	}
	if value := cleanString(signed, nil); value != signed {
		t.Fatal("signed URL changed during debug sanitization")
	}
}

func TestCredentialDependentPlaybackURLIsNotMadePlayableBySanitization(t *testing.T) {
	capture := &rawCapture{Stories: map[string]*storyEvidence{}, Secrets: []string{"LOCAL_SESSION_SECRET"}, DebugDir: t.TempDir()}
	story := decodeStory(t, `{"post_id":"22","attachments":[
		{"media":{"__typename":"Video","id":"v22","playable_url":"https://video.xx.fbcdn.net/needs-token.mp4?access_token=credential-value"}},
		{"media":{"__typename":"Video","id":"v23","playable_url":"https://video.xx.fbcdn.net/LOCAL%5fSESSION%5fSECRET.mp4"}},
		{"media":{"__typename":"Video","id":"v24","playable_url":"https://video.xx.fbcdn.net/movie.mp4?oh=abc;access_token=credential-value","request_headers":{"X-Debug":"synthetic-header-value"}}}
	]}`)
	capture.add(story)
	record := capture.Stories["22"]
	if record == nil || len(record.Videos) != 3 {
		t.Fatal(record)
	}
	for _, video := range record.Videos {
		if video.PlaybackURL != "" || video.PlaybackURLEphemeral {
			t.Fatalf("credential-dependent URL retained: %+v", video)
		}
	}
	debug, err := os.ReadFile(record.DebugFile)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(debug), "LOCAL_SESSION_SECRET") || strings.Contains(string(debug), "LOCAL%5fSESSION%5fSECRET") || strings.Contains(string(debug), "credential-value") || strings.Contains(string(debug), "synthetic-header-value") {
		t.Fatal("debug evidence leaked a credential")
	}
}
