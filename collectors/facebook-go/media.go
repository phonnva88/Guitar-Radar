package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// The upstream Post model discards most media fields. Capture only GraphQL
// story nodes, in memory, before that model is decoded; never capture bootstrap
// HTML, request forms, cookies or headers.
type captureKey struct{}

type imageCandidate struct {
	URL        string `json:"url"`
	Width      any    `json:"width"`
	Height     any    `json:"height"`
	Source     string `json:"source"`
	Path       string `json:"path"`
	PhotoID    string `json:"photo_id,omitempty"`
	photoScope string
}

// A playable URL is only exported when a Video/Reel node explicitly contains
// one. Facebook CDN URLs may expire and are never a post permalink.
type videoCandidate struct {
	VideoID              string   `json:"video_id,omitempty"`
	Kind                 string   `json:"kind"`
	ThumbnailURL         string   `json:"thumbnail_url,omitempty"`
	DurationSeconds      *float64 `json:"duration_seconds,omitempty"`
	Width                any      `json:"width,omitempty"`
	Height               any      `json:"height,omitempty"`
	PlaybackURL          string   `json:"playback_url,omitempty"`
	PlaybackURLExpiresAt string   `json:"playback_url_expires_at,omitempty"`
	PlaybackURLEphemeral bool     `json:"playback_url_ephemeral"`
	Path                 string   `json:"path"`
}

type storyEvidence struct {
	Story        map[string]any
	Images       []imageCandidate
	Album        albumPreview
	Videos       []videoCandidate
	PossibleType string
	TypeEvidence []string
	DebugFile    string
}

type rawCapture struct {
	Stories        map[string]*storyEvidence
	Secrets        []string
	DebugDir       string
	Err            bool
	PageStoryCount int
	InvalidCursor  bool
	FeedSeen       bool
	PageInfoSeen   bool
	ReadAlbum      func(context.Context, string, albumPreview, []string) (albumResult, error)
}

type captureTransport struct {
	Base         http.RoundTripper
	Capture      *rawCapture
	albumHeaders http.Header
}

func (t *captureTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	base := t.Base
	if base == nil {
		base = http.DefaultTransport
	}
	resp, err := base.RoundTrip(req)
	if err != nil || resp == nil || resp.Body == nil {
		return resp, err
	}
	if resp.StatusCode == http.StatusOK && req.Method == http.MethodGet &&
		req.URL.Scheme == "https" && req.URL.Host == "www.facebook.com" &&
		strings.TrimRight(req.URL.Path, "/") == "/groups/feed" {
		t.albumHeaders = req.Header.Clone()
	}
	if req.Method != http.MethodPost || strings.TrimRight(req.URL.Path, "/") != "/api/graphql" {
		return resp, nil
	}
	const maxBody = 32 << 20
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxBody+1))
	resp.Body.Close()
	if err != nil || len(body) > maxBody {
		return nil, errors.New("bounded GraphQL response could not be read")
	}
	resp.Body = io.NopCloser(bytes.NewReader(body))
	if resp.StatusCode == 200 {
		t.Capture.consume(body)
	}
	return resp, nil
}

func captureContext(ctx context.Context, capture *rawCapture) context.Context {
	return context.WithValue(ctx, captureKey{}, capture)
}

func (c *rawCapture) consume(body []byte) {
	for _, line := range bytes.Split(body, []byte("\n")) {
		line = bytes.TrimSpace(line)
		line = bytes.TrimPrefix(line, []byte("for(;;);"))
		line = bytes.TrimPrefix(line, []byte("for (;;);"))
		var obj map[string]any
		dec := json.NewDecoder(bytes.NewReader(line))
		dec.UseNumber()
		if dec.Decode(&obj) != nil {
			continue
		}
		findPageInfo(obj, func(info map[string]any) {
			c.PageInfoSeen = true
			if info["has_next_page"] == true && scalarString(info["end_cursor"]) == "" {
				c.InvalidCursor = true
			}
		})
		findGroupFeed(obj, func() { c.FeedSeen = true })
		// Only group_feed edges and explicit streamed edge payloads are accepted.
		findFeedStories(obj, func(story map[string]any) { c.add(story) })
		findStreamedStories(obj, func(story map[string]any) { c.add(story) })
	}
}

func findStreamedStories(value any, found func(map[string]any)) {
	switch v := value.(type) {
	case map[string]any:
		if path, ok := v["path"].([]any); ok && hasPathPart(path, "group_feed") && hasPathPart(path, "edges") {
			if data, ok := v["data"].(map[string]any); ok {
				if node, ok := data["node"].(map[string]any); ok {
					found(node)
				}
			}
		}
		if incremental, ok := v["incremental"].([]any); ok {
			for _, child := range incremental {
				findStreamedStories(child, found)
			}
		}
	case []any:
		for _, child := range v {
			findStreamedStories(child, found)
		}
	}
}

func findPageInfo(value any, found func(map[string]any)) {
	switch v := value.(type) {
	case map[string]any:
		if feed, ok := v["group_feed"].(map[string]any); ok {
			if info, ok := feed["page_info"].(map[string]any); ok {
				found(info)
			}
			return
		}
		if path, ok := v["path"].([]any); ok && hasPathPart(path, "group_feed") {
			if data, ok := v["data"].(map[string]any); ok {
				if info, ok := data["page_info"].(map[string]any); ok {
					found(info)
				}
			}
		}
		for _, child := range v {
			findPageInfo(child, found)
		}
	case []any:
		for _, child := range v {
			findPageInfo(child, found)
		}
	}
}

func findGroupFeed(value any, found func()) {
	switch v := value.(type) {
	case map[string]any:
		if _, ok := v["group_feed"].(map[string]any); ok {
			found()
			return
		}
		for _, child := range v {
			findGroupFeed(child, found)
		}
	case []any:
		for _, child := range v {
			findGroupFeed(child, found)
		}
	}
}

func hasPathPart(path []any, part string) bool {
	for _, p := range path {
		if p == part {
			return true
		}
	}
	return false
}

func findFeedStories(value any, found func(map[string]any)) {
	switch v := value.(type) {
	case map[string]any:
		if feed, ok := v["group_feed"].(map[string]any); ok {
			if edges, ok := feed["edges"].([]any); ok {
				for _, edge := range edges {
					if e, ok := edge.(map[string]any); ok {
						if node, ok := e["node"].(map[string]any); ok {
							found(node)
						}
					}
				}
			}
			return // Do not mistake nested recommended/shared stories for feed posts.
		}
		for _, child := range v {
			findFeedStories(child, found)
		}
	case []any:
		for _, child := range v {
			findFeedStories(child, found)
		}
	}
}

var safePostFile = regexp.MustCompile(`^[A-Za-z0-9_-]{1,200}$`)
var postURLID = regexp.MustCompile(`/(?:posts|permalink)/([0-9]+)|[?&]story_fbid=([0-9]+)`)

func scalarString(value any) string {
	switch v := value.(type) {
	case string:
		return v
	case json.Number:
		return v.String()
	}
	return ""
}

func ownPostID(story map[string]any) string {
	if id := scalarString(story["post_id"]); id != "" {
		return id
	}
	var found string
	var walk func(any, int)
	walk = func(value any, depth int) {
		if depth > 24 || found != "" {
			return
		}
		switch v := value.(type) {
		case map[string]any:
			for _, key := range []string{"permalink_url", "url", "wwwURL"} {
				if m := postURLID.FindStringSubmatch(scalarString(v[key])); m != nil {
					found = m[1] + m[2]
					return
				}
			}
			keys := make([]string, 0, len(v))
			for key := range v {
				keys = append(keys, key)
			}
			sort.Strings(keys)
			for _, key := range keys {
				if !excludedMediaKeys[strings.ToLower(key)] {
					walk(v[key], depth+1)
				}
			}
		case []any:
			for _, child := range v {
				walk(child, depth+1)
			}
		}
	}
	walk(story, 0)
	return found
}

func (c *rawCapture) add(story map[string]any) {
	typ := scalarString(story["__typename"])
	if typ != "" && typ != "Story" {
		return
	}
	c.PageStoryCount++
	id := ownPostID(story)
	if id == "" {
		id = scalarString(story["id"])
	}
	if id == "" {
		return
	}
	clean, ok := sanitize(story, c.Secrets).(map[string]any)
	if !ok {
		return
	}
	images := extractImages(clean)
	// Inspect the original URL first: sanitizing a credential-dependent media
	// URL by removing its query key could create a broken-looking playable URL.
	videos := extractVideos(story)
	for i := range videos {
		videos[i] = sanitizeVideoCandidate(videos[i], c.Secrets)
	}
	possible, evidence := classifyStory(clean, images, videos)
	record := &storyEvidence{Story: clean, Images: images, Album: incompleteAlbum(clean), Videos: videos, PossibleType: possible, TypeEvidence: evidence}
	previous := c.Stories[id]
	if previous != nil {
		record.Images = deduplicateImages(append(record.Images, previous.Images...))
		if record.Album.count == 0 {
			record.Album = previous.Album
		}
		record.Videos = deduplicateVideos(append(record.Videos, previous.Videos...))
		record.PossibleType, record.TypeEvidence = classifyStory(clean, record.Images, record.Videos)
		if record.PossibleType == "unknown" && previous.PossibleType != "unknown" {
			record.PossibleType, record.TypeEvidence = previous.PossibleType, previous.TypeEvidence
		}
	}
	if c.DebugDir != "" && safePostFile.MatchString(id) {
		path := filepath.Join(c.DebugDir, id+".json")
		// A run directory is exclusive. Preserve the first observation instead of
		// replacing previous response evidence for a repeated post.
		for part := 2; ; part++ {
			if _, err := os.Stat(path); os.IsNotExist(err) {
				break
			} else if err != nil {
				c.Err = true
				break
			}
			path = filepath.Join(c.DebugDir, fmt.Sprintf("%s.part-%d.json", id, part))
		}
		if _, err := os.Stat(path); os.IsNotExist(err) {
			payload := map[string]any{"post_id": id, "collector": "facebook-go", "story": clean, "image_candidates": record.Images, "video_candidates": record.Videos, "possible_type": record.PossibleType, "type_evidence": record.TypeEvidence}
			if previous != nil && previous.DebugFile != "" {
				payload["previous_debug_file"] = previous.DebugFile
			}
			if err := writeJSON(path, payload); err != nil {
				c.Err = true
			} else {
				record.DebugFile = path
			}
		} else if err == nil {
			record.DebugFile = path
		} else {
			c.Err = true
		}
	}
	c.Stories[id] = record
	if storyID := scalarString(story["id"]); storyID != "" {
		c.Stories[storyID] = record
	}
}

func sensitiveKey(key string) bool {
	k := strings.ToLower(key)
	if strings.HasPrefix(k, "__") && k != "__typename" {
		return true
	}
	for _, part := range []string{"token", "cookie", "authorization", "passwd", "password", "credential", "session", "tracking", "encrypt", "csrf", "fb_dtsg", "jazoest", "header", "api_key", "api-key", "secret"} {
		if strings.Contains(k, part) {
			return true
		}
	}
	switch k {
	case "xs", "c_user", "datr", "sb", "fr", "lsd", "headers", "request", "extensions":
		return true
	}
	return false
}

var credentialText = regexp.MustCompile(`(?i)(?:cookie|authorization|access_token|fb_dtsg|lsd|xs|c_user)\s*[:=]\s*[^\s;&"']+`)

func cleanString(value string, secrets []string) string {
	if safeFBMediaURL(value) != "" {
		if containsMediaSecret(value, secrets) {
			return ""
		}
		return value
	}
	for _, secret := range secrets {
		if secret != "" {
			value = strings.ReplaceAll(value, secret, "[REDACTED]")
		}
	}
	if u, err := url.Parse(value); err == nil && (u.Scheme == "https" || u.Scheme == "http") {
		if containsMediaSecret(value, secrets) && !strings.Contains(value, "[REDACTED]") {
			return ""
		}
		u.User = nil
		q := u.Query()
		for key := range q {
			if sensitiveKey(key) {
				q.Del(key)
			}
		}
		u.RawQuery = q.Encode()
		if sensitiveKey(u.Fragment) {
			u.Fragment = ""
		}
		value = u.String()
		return value
	}
	return credentialText.ReplaceAllString(value, "[REDACTED]")
}

func sanitize(value any, secrets []string) any {
	switch v := value.(type) {
	case map[string]any:
		out := map[string]any{}
		for key, child := range v {
			if !sensitiveKey(key) {
				out[key] = sanitize(child, secrets)
			}
		}
		return out
	case []any:
		out := make([]any, 0, len(v))
		for _, child := range v {
			out = append(out, sanitize(child, secrets))
		}
		return out
	case string:
		return cleanString(v, secrets)
	default:
		return value
	}
}

var excludedMediaKeys = map[string]bool{"actors": true, "actor": true, "author": true, "owner": true, "owning_profile": true, "profile_picture": true, "profile_image": true, "avatar": true, "comments": true, "comment": true, "feedback": true, "attached_story": true, "attached_story_layout": true, "recommended": true, "suggested_stories": true, "link_preview": true, "external_link": true}

func extractImages(story map[string]any) []imageCandidate {
	result := []imageCandidate{}
	var walk func(any, string, bool, bool, string, string, int)
	walk = func(value any, path string, attachment, photo bool, photoID, photoScope string, depth int) {
		if depth > 32 {
			return
		}
		switch v := value.(type) {
		case map[string]any:
			typ := strings.ToLower(scalarString(v["__typename"]))
			if strings.Contains(typ, "video") || strings.Contains(typ, "reel") || externalMedia(v) || attachmentStyle(v, "share") {
				return
			}
			photo = photo || typ == "photo" || typ == "image"
			if typ == "photo" {
				// Every nested Photo owns its renditions. Album/attachment IDs
				// must never become the identity of all their child photos.
				photoID, photoScope = scalarString(v["id"]), path
			}
			if photo {
				for _, key := range []string{"uri", "url"} {
					u := scalarString(v[key])
					parsed, err := url.Parse(u)
					// A Photo.url is commonly its Facebook permalink, not image bytes.
					if err == nil && parsed.Hostname() != "" && parsed.User == nil && (parsed.Scheme == "https" || parsed.Scheme == "http") && !isFacebookPermalink(parsed) {
						source := "media"
						if attachment {
							source = "attachment"
						}
						result = append(result, imageCandidate{URL: u, Width: dimension(v["width"]), Height: dimension(v["height"]), Source: source, Path: path + "." + key, PhotoID: photoID, photoScope: photoScope})
					}
				}
			}
			keys := make([]string, 0, len(v))
			for key := range v {
				keys = append(keys, key)
			}
			sort.Strings(keys)
			media, _ := v["media"].(map[string]any)
			videoAttachment := explicitVideoType(strings.ToLower(scalarString(media["__typename"])))
			for _, key := range keys {
				lower := strings.ToLower(key)
				if excludedMediaKeys[lower] || lower == "shared_story" || lower == "video" || lower == "reel" || lower == "short_form_video_context" || strings.Contains(lower, "actor_photo") || strings.Contains(lower, "actor_picture") || strings.Contains(lower, "profile") || strings.Contains(lower, "avatar") || strings.Contains(lower, "thumbnail") || strings.Contains(lower, "preview") || strings.Contains(lower, "poster") {
					continue
				}
				inAttachment := attachment || lower == "attachments" || lower == "subattachments"
				imageField := lower == "image" || lower == "images" || lower == "photo_image" || lower == "viewer_image" || lower == "photos"
				if videoAttachment && imageField && lower != "photos" {
					continue
				}
				imageContext := photo || ((inAttachment || strings.Contains(path, ".media")) && imageField)
				walk(v[key], path+"."+key, inAttachment, imageContext, photoID, photoScope, depth+1)
			}
		case []any:
			for i, child := range v {
				walk(child, fmt.Sprintf("%s[%d]", path, i), attachment, photo, photoID, photoScope, depth+1)
			}
		}
	}
	walk(story, "story", false, false, "", "", 0)
	return deduplicateImages(result)
}

func isFacebookPermalink(u *url.URL) bool {
	h := strings.ToLower(u.Hostname())
	return h == "facebook.com" || strings.HasSuffix(h, ".facebook.com") || h == "fb.com"
}
func dimension(v any) any {
	if n, ok := v.(json.Number); ok {
		if i, err := n.Int64(); err == nil && i > 0 {
			return i
		}
	}
	if f, ok := v.(float64); ok && f > 0 {
		return int64(f)
	}
	return nil
}

func classifyStory(story map[string]any, images []imageCandidate, videos []videoCandidate) (string, []string) {
	if len(videos) > 0 {
		kind := videos[0].Kind
		typ := map[string]string{"direct": "video", "reel": "reel", "shared": "shared_video", "live": "live_video"}[kind]
		return typ, []string{"explicit video/reel media metadata at " + videos[0].Path}
	}
	for _, pair := range [][2]string{{"is_anonymous", "anonymous_post"}, {"is_anonymous_post", "anonymous_post"}, {"is_announcement", "announcement"}, {"is_suggested", "suggested_post"}} {
		if story[pair[0]] == true {
			return pair[1], []string{pair[0] + "=true"}
		}
	}
	if story["attached_story"] != nil || story["attached_story_layout"] != nil {
		return "shared_post", []string{"attached_story"}
	}
	if len(images) > 0 {
		return "photo_post", []string{"proven photo/image metadata"}
	}
	var detected string
	var walk func(any, bool, int)
	walk = func(v any, attachment bool, depth int) {
		if depth > 24 || detected != "" {
			return
		}
		switch obj := v.(type) {
		case map[string]any:
			if attachment && externalMedia(obj) {
				detected = "external_link_preview"
				return
			}
			typ := strings.ToLower(scalarString(obj["__typename"]))
			if strings.Contains(typ, "marketplace") {
				detected = "marketplace_style_post"
			}
			for _, key := range sortedKeys(obj) {
				lower := strings.ToLower(key)
				if !excludedMediaKeys[lower] {
					walk(obj[key], attachment || lower == "attachment" || lower == "attachments", depth+1)
				}
			}
		case []any:
			for _, c := range obj {
				walk(c, attachment, depth+1)
			}
		}
	}
	walk(story, false, 0)
	if detected != "" {
		return detected, []string{"explicit media typename"}
	}
	return "unknown", []string{}
}
