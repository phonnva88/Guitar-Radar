package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPhotoRenditionsDoNotDoubleGalleryCount(t *testing.T) {
	// Synthetic regression for a four-photo gallery exposing two sizes per Photo.
	photos := []string{}
	for i := 1; i <= 4; i++ {
		photos = append(photos, fmt.Sprintf(`{"media":{"__typename":"Photo","id":"%d","image":{"uri":"https://cdn.example/%d?size=small","width":320,"height":240},"viewer_image":{"uri":"https://cdn.example/%d?size=large&oh=SIGNED","width":1600,"height":1200}}}`, i, i, i))
	}
	images := extractImages(decodeStory(t, `{"attachments":[{"subattachments":{"nodes":[`+strings.Join(photos, ",")+`]}}]}`))
	if len(images) != 4 {
		t.Fatalf("four logical photos became %d candidates: %+v", len(images), images)
	}
	for i, image := range images {
		if image.PhotoID != fmt.Sprint(i+1) || image.Width != int64(1600) || !strings.HasSuffix(image.URL, "?size=large&oh=SIGNED") || !strings.HasSuffix(image.Path, ".viewer_image.uri") {
			t.Fatalf("wrong rendition/evidence: %+v", image)
		}
	}
}

func TestPhotoIdentityAndNestedScope(t *testing.T) {
	tests := []struct {
		name, story string
		count       int
	}{
		{"same photo across layouts", `{"attachments":[{"media":{"__typename":"Photo","id":"1","image":{"uri":"https://cdn.example/a"}}},{"media":{"__typename":"Photo","id":"1","viewer_image":{"uri":"https://cdn.example/b"}}}]}`, 1},
		{"idless photo renditions", `{"attachments":[{"media":{"__typename":"Photo","image":{"uri":"https://cdn.example/a?size=1"},"viewer_image":{"uri":"https://cdn.example/a?size=2"}}}]}`, 1},
		{"duplicate idless nodes", `{"attachments":[{"media":{"__typename":"Photo","image":{"uri":"https://cdn.example/a"}}},{"media":{"__typename":"Photo","image":{"uri":"https://cdn.example/a"}}}]}`, 1},
		{"distinct photo ids", `{"attachments":[{"media":{"__typename":"Photo","id":"1","image":{"uri":"https://cdn.example/same"}}},{"media":{"__typename":"Photo","id":"2","image":{"uri":"https://cdn.example/same"}}}]}`, 2},
		{"parent album id is not child identity", `{"attachments":[{"media":{"__typename":"PhotoAttachment","id":"album","photos":[{"__typename":"Photo","id":"1","image":{"uri":"https://cdn.example/a"}},{"__typename":"Photo","id":"2","image":{"uri":"https://cdn.example/b"}}]}}]}`, 2},
		{"nested photo resets inherited id", `{"attachments":[{"media":{"__typename":"Photo","id":"1","image":{"uri":"https://cdn.example/a"},"photos":[{"__typename":"Photo","image":{"uri":"https://cdn.example/b"},"viewer_image":{"uri":"https://cdn.example/b?large"}}]}}]}`, 2},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			images := extractImages(decodeStory(t, test.story))
			if len(images) != test.count {
				t.Fatalf("got %d, wanted %d: %+v", len(images), test.count, images)
			}
			serialized, _ := json.Marshal(images)
			if strings.Contains(string(serialized), "photoScope") || strings.Contains(string(serialized), "scope:") {
				t.Fatal("internal scope leaked into output")
			}
		})
	}
}

func TestCDNPhotoFallbackIsConservativeAndKeepsSignedURL(t *testing.T) {
	first := "https://scontent.fsgn2-9.fna.fbcdn.net/v/t39.30808-6/897384301_29234409696195716_4771406257957693077_n.jpg?stp=small&oh=FIRST"
	best := "https://scontent.fsgn2-1.fna.fbcdn.net/v/t39.30808-6/897384301_29234409696195716_4771406257957693077_n.jpg?stp=large&oh=SIGNED"
	images := deduplicateImages([]imageCandidate{
		{URL: first, Width: int64(320), Height: int64(240)},
		{URL: best, Width: int64(1600), Height: int64(1200)},
	})
	if len(images) != 1 || images[0].URL != best {
		t.Fatalf("same CDN object not merged or signed URL changed: %+v", images)
	}
	for _, urls := range [][2]string{
		{"https://cdn.example/image?id=1", "https://cdn.example/image?id=2"},
		{"https://fbcdn.net.evil.example/1_2_3_n.jpg?x=1", "https://fbcdn.net.evil.example/1_2_3_n.jpg?x=2"},
		{"https://scontent.fbcdn.net/image?id=1", "https://scontent.fbcdn.net/image?id=2"},
		{first, strings.Replace(first, "897384301_", "897384302_", 1)},
	} {
		if got := deduplicateImages([]imageCandidate{{URL: urls[0]}, {URL: urls[1]}}); len(got) != 2 {
			t.Fatalf("unproven identities merged: %+v", got)
		}
	}
}

func TestRepeatedCaptureKeepsOneBestPhotoRendition(t *testing.T) {
	capture := &rawCapture{Stories: map[string]*storyEvidence{}}
	story := func(uri string, size int) map[string]any {
		return decodeStory(t, fmt.Sprintf(`{"post_id":"999","attachments":[{"media":{"__typename":"Photo","id":"photo-1","image":{"uri":%q,"width":%d,"height":%d}}}]}`, uri, size, size))
	}
	capture.add(story("https://cdn.example/big?oh=SIGNED", 1600))
	capture.add(story("https://cdn.example/small?oh=NEW", 320))
	images := capture.Stories["999"].Images
	if len(images) != 1 || images[0].URL != "https://cdn.example/big?oh=SIGNED" {
		t.Fatalf("repeated response duplicated/downgraded photo: %+v", images)
	}
}

func TestReportedPostDuplicatePhotoRenditions(t *testing.T) {
	file, err := os.Open(filepath.Join("..", "..", "tests", "fixtures", "duplicate-photo-renditions.json"))
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	var fixture struct {
		PostID             string `json:"post_id"`
		ExpectedImageCount int    `json:"expected_image_count"`
		Candidates         []struct {
			URL    string      `json:"url"`
			Width  json.Number `json:"width"`
			Height json.Number `json:"height"`
			Source string      `json:"source"`
			Path   string      `json:"path"`
		} `json:"image_candidates"`
	}
	decoder := json.NewDecoder(file)
	decoder.UseNumber()
	if err := decoder.Decode(&fixture); err != nil {
		t.Fatal(err)
	}
	if fixture.PostID != "29373505755576464" || len(fixture.Candidates) != 8 {
		t.Fatalf("unexpected regression fixture: %+v", fixture)
	}
	candidates := make([]imageCandidate, 0, len(fixture.Candidates))
	for _, candidate := range fixture.Candidates {
		candidates = append(candidates, imageCandidate{
			URL: candidate.URL, Width: dimension(candidate.Width), Height: dimension(candidate.Height),
			Source: candidate.Source, Path: candidate.Path,
		})
	}
	images := deduplicateImages(candidates)
	if len(images) != fixture.ExpectedImageCount {
		t.Fatalf("post %s returned %d photos, wanted %d", fixture.PostID, len(images), fixture.ExpectedImageCount)
	}
	for index, image := range images {
		best := fixture.Candidates[index*2+1]
		if image.URL != best.URL || image.Width != int64(1536) || image.Height != int64(2048) || image.Source != best.Source || image.Path != best.Path {
			t.Fatalf("post %s rendition/evidence mismatch: %+v", fixture.PostID, image)
		}
	}
}
