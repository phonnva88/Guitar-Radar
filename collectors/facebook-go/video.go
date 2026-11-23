package main

import (
	"encoding/json"
	"fmt"
	"math"
	"net/url"
	"regexp"
	"sort"
	"strings"
	"time"
)

// Video extraction is scoped to media in the feed story or its explicitly
// attached shared story. A link preview, recommendation, or text mentioning a
// video is not evidence of a Facebook video attached to this post.
func extractVideos(story map[string]any) []videoCandidate {
	result := []videoCandidate{}
	var walk func(any, string, bool, bool, bool, int)
	walk = func(value any, path string, media, shared, reel bool, depth int) {
		if depth > 32 {
			return
		}
		switch v := value.(type) {
		case map[string]any:
			if externalMedia(v) {
				return
			}
			typ := strings.ToLower(scalarString(v["__typename"]))
			shared = shared || (media && attachmentStyle(v, "share"))
			start := len(result)
			if media && explicitVideoType(typ) {
				// A typed Reel wrapper may carry its metadata on the child Video.
				if nested, ok := v["video"].(map[string]any); ok && explicitVideoType(strings.ToLower(scalarString(nested["__typename"]))) && isReel(typ) {
					walk(nested, path+".video", true, shared, true, depth+1)
				}
				if len(result) == start {
					result = append(result, videoFromNode(v, path, shared, reel))
				}
				return
			}
			for _, key := range sortedKeys(v) {
				lower := strings.ToLower(key)
				if sensitiveKey(lower) ||
					(excludedMediaKeys[lower] && lower != "attached_story" && lower != "attached_story_layout") ||
					strings.Contains(lower, "profile") || strings.Contains(lower, "avatar") ||
					strings.Contains(lower, "recommend") || strings.Contains(lower, "suggest") ||
					strings.Contains(lower, "related") || lower == "replies" ||
					strings.Contains(lower, "thumbnail") || strings.Contains(lower, "preview") ||
					lower == "external_link" {
					continue
				}
				childShared := shared || lower == "attached_story" || lower == "attached_story_layout" || lower == "shared_story"
				childMedia := media || lower == "media" || lower == "video" || lower == "attachment" || lower == "attachments" || lower == "subattachments" || lower == "all_subattachments"
				childReel := reel || lower == "reel" || lower == "short_form_video_context"
				walk(v[key], path+"."+key, childMedia, childShared, childReel, depth+1)
			}
		case []any:
			for i, child := range v {
				walk(child, fmt.Sprintf("%s[%d]", path, i), media, shared, reel, depth+1)
			}
		}
	}
	walk(story, "story", false, false, false, 0)
	return deduplicateVideos(result)
}

func sortedKeys(value map[string]any) []string {
	keys := make([]string, 0, len(value))
	for key := range value {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func externalMedia(node map[string]any) bool {
	typ := strings.ToLower(scalarString(node["__typename"]))
	if strings.Contains(typ, "external") || strings.Contains(typ, "linkattachment") || typ == "linkpreview" || typ == "webpage" || typ == "link" {
		return true
	}
	if target, ok := node["target"].(map[string]any); ok {
		if typ := strings.ToLower(scalarString(target["__typename"])); typ == "externalurl" || typ == "webpage" {
			return true
		}
	}
	if styles, ok := node["style_list"].([]any); ok {
		for _, style := range styles {
			s := strings.ToLower(scalarString(style))
			if strings.Contains(s, "external") || s == "link" {
				return true
			}
		}
	}
	return false
}

func attachmentStyle(node map[string]any, name string) bool {
	styles, _ := node["style_list"].([]any)
	for _, style := range styles {
		if strings.EqualFold(scalarString(style), name) {
			return true
		}
	}
	return false
}

func explicitVideoType(typ string) bool {
	switch typ {
	case "video", "reel", "reelsvideo", "livevideo", "broadcastvideo", "shortformvideo":
		return true
	}
	return false
}

func isReel(typ string) bool {
	return typ == "reel" || typ == "reelsvideo" || typ == "shortformvideo"
}

var videoIDPattern = regexp.MustCompile(`^[A-Za-z0-9_+=:/-]{1,160}$`)
var nonVideoPath = regexp.MustCompile(`(?i)\.(?:jpe?g|png|gif|webp|avif|svg|mpd)$`)

func videoFromNode(node map[string]any, path string, shared, reel bool) videoCandidate {
	typ := strings.ToLower(scalarString(node["__typename"]))
	kind := "direct"
	if shared {
		kind = "shared"
	} else if strings.Contains(typ, "live") || typ == "broadcastvideo" || node["is_live_streaming"] == true || node["is_live_video"] == true || node["is_live"] == true || liveStatus(node) {
		kind = "live"
	} else if reel || isReel(typ) || node["is_reel"] == true {
		kind = "reel"
	}
	id := scalarString(node["id"])
	if id == "" {
		id = scalarString(node["video_id"])
	}
	if !videoIDPattern.MatchString(id) {
		id = ""
	}
	playback := ""
	for _, key := range []string{"playable_url_quality_hd", "playable_url", "browser_native_hd_url", "browser_native_sd_url", "hd_src", "sd_src"} {
		if source := firstMediaURL(node, key); source != "" {
			parsed, _ := url.Parse(source)
			if !nonVideoPath.MatchString(parsed.Path) {
				playback = source
				break
			}
		}
	}
	thumb := firstMediaURL(node, "thumbnail_url")
	if thumb == "" {
		for _, key := range []string{"preferred_thumbnail", "thumbnail_image", "thumbnailImage", "image", "first_frame_thumbnail"} {
			if thumb = imageURL(node[key]); thumb != "" {
				break
			}
		}
	}
	width, height := dimension(node["width"]), dimension(node["height"])
	for _, key := range []string{"original_dimensions", "originalDimensions", "dimensions"} {
		if dims, ok := node[key].(map[string]any); ok {
			if d := dimension(dims["width"]); d != nil {
				width = d
			}
			if d := dimension(dims["height"]); d != nil {
				height = d
			}
			break
		}
	}
	duration := positiveNumber(node["duration_seconds"], 7*24*3600)
	if duration == nil {
		duration = positiveNumber(node["video_duration"], 7*24*3600)
	}
	if duration == nil {
		duration = positiveNumber(node["duration"], 7*24*3600)
	}
	if duration == nil {
		if ms := positiveNumber(node["playable_duration_in_ms"], 7*24*3600*1000); ms != nil {
			seconds := *ms / 1000
			duration = &seconds
		}
	}
	candidate := videoCandidate{VideoID: id, Kind: kind, ThumbnailURL: thumb, Width: width, Height: height, DurationSeconds: duration,
		PlaybackURL: playback, PlaybackURLEphemeral: playback != "", Path: path}
	if playback != "" {
		candidate.PlaybackURLExpiresAt = explicitExpiry(node)
	}
	return candidate
}

func liveStatus(node map[string]any) bool {
	for _, key := range []string{"live_status", "broadcast_status"} {
		switch strings.ToUpper(scalarString(node[key])) {
		case "LIVE", "LIVE_STARTED", "LIVE_ENDED", "ENDED", "VOD":
			return true
		}
	}
	return false
}

func firstMediaURL(node map[string]any, keys ...string) string {
	for _, key := range keys {
		if value := safeFBMediaURL(scalarString(node[key])); value != "" {
			return value
		}
	}
	return ""
}

func imageURL(value any) string {
	if s := safeFBMediaURL(scalarString(value)); s != "" {
		return s
	}
	node, ok := value.(map[string]any)
	if !ok {
		return ""
	}
	if s := firstMediaURL(node, "uri", "url"); s != "" {
		return s
	}
	return imageURL(node["image"])
}

func safeFBMediaURL(value string) string {
	u, err := url.Parse(value)
	if err != nil || u.Scheme != "https" || u.User != nil || u.Host != u.Hostname() || u.Path == "" ||
		strings.ContainsAny(value, "#\\\"'<>\x7f ") || strings.Contains(value, "[REDACTED]") {
		return ""
	}
	host := strings.ToLower(u.Hostname())
	if !facebookCDNHost.MatchString(host) {
		return ""
	}
	query, err := url.ParseQuery(u.RawQuery)
	if err != nil {
		return ""
	}
	for key := range query {
		if sensitiveKey(key) || strings.HasPrefix(key, "__") {
			return ""
		}
	}
	return value
}

var facebookCDNHost = regexp.MustCompile(`^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+fbcdn\.net$`)

func containsMediaSecret(value string, secrets []string) bool {
	decoded, _ := url.QueryUnescape(value)
	for _, secret := range secrets {
		if secret != "" && (strings.Contains(value, secret) || strings.Contains(decoded, secret)) {
			return true
		}
	}
	return false
}

func sanitizeVideoCandidate(value videoCandidate, secrets []string) videoCandidate {
	if cleaned := cleanString(value.VideoID, secrets); cleaned != value.VideoID {
		value.VideoID = ""
	}
	if containsMediaSecret(value.ThumbnailURL, secrets) {
		value.ThumbnailURL = ""
	}
	if containsMediaSecret(value.PlaybackURL, secrets) {
		value.PlaybackURL = ""
		value.PlaybackURLExpiresAt = ""
		value.PlaybackURLEphemeral = false
	}
	if containsMediaSecret(value.Path, secrets) {
		value.Path = "story.media"
	}
	return value
}

func positiveNumber(value any, maximum float64) *float64 {
	var n float64
	switch v := value.(type) {
	case json.Number:
		parsed, err := v.Float64()
		if err != nil {
			return nil
		}
		n = parsed
	case float64:
		n = v
	case int:
		n = float64(v)
	case int64:
		n = float64(v)
	default:
		return nil
	}
	if math.IsNaN(n) || n < 0 || n > maximum {
		return nil
	}
	return &n
}

func explicitExpiry(node map[string]any) string {
	for _, key := range []string{"playback_url_expires_at", "playable_url_expires_at", "playable_url_expiration_time"} {
		value := node[key]
		if s := scalarString(value); s != "" {
			if instant, err := time.Parse(time.RFC3339, s); err == nil {
				return instant.UTC().Format(time.RFC3339)
			}
			if number, err := json.Number(s).Int64(); err == nil && number > 0 {
				return time.Unix(number, 0).UTC().Format(time.RFC3339)
			}
		}
	}
	return ""
}

func deduplicateVideos(values []videoCandidate) []videoCandidate {
	result := make([]videoCandidate, 0, len(values))
	index := map[string]int{}
	for _, candidate := range values {
		key := candidate.VideoID
		if key == "" {
			key = candidate.PlaybackURL
		}
		if key == "" {
			key = candidate.Path
		}
		if position, ok := index[key]; ok {
			prior := &result[position]
			if prior.ThumbnailURL == "" {
				prior.ThumbnailURL = candidate.ThumbnailURL
			}
			if prior.PlaybackURL == "" {
				prior.PlaybackURL = candidate.PlaybackURL
				prior.PlaybackURLExpiresAt = candidate.PlaybackURLExpiresAt
				prior.PlaybackURLEphemeral = candidate.PlaybackURLEphemeral
			}
			if prior.DurationSeconds == nil {
				prior.DurationSeconds = candidate.DurationSeconds
			}
			if prior.Width == nil {
				prior.Width = candidate.Width
			}
			if prior.Height == nil {
				prior.Height = candidate.Height
			}
			continue
		}
		index[key] = len(result)
		result = append(result, candidate)
	}
	return result
}
