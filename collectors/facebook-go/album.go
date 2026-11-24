package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/teslashibe/facebook-go/groups"
)

type albumPreview struct {
	count int
	ids   map[string]bool
}

func incompleteAlbum(story map[string]any) albumPreview {
	var preview albumPreview
	sections, _ := story["comet_sections"].(map[string]any)
	content, _ := sections["content"].(map[string]any)
	contentStory, _ := content["story"].(map[string]any)
	for _, parent := range []map[string]any{story, contentStory} {
		attachments, _ := parent["attachments"].([]any)
		for _, value := range attachments {
			attachment, _ := value.(map[string]any)
			style, _ := attachment["styles"].(map[string]any)
			renderer := scalarString(style["__typename"])
			if !strings.HasPrefix(renderer, "StoryAttachmentAlbum") || !strings.HasSuffix(renderer, "StyleRenderer") {
				continue
			}
			inner, _ := style["attachment"].(map[string]any)
			list, _ := inner["all_subattachments"].(map[string]any)
			count, _ := dimension(list["count"]).(int64)
			nodes, _ := list["nodes"].([]any)
			ids := make(map[string]bool, len(nodes))
			for _, value := range nodes {
				item, _ := value.(map[string]any)
				media, _ := item["media"].(map[string]any)
				if id := scalarString(media["id"]); numericID.MatchString(id) {
					ids[id] = true
				}
			}
			if len(ids) > 0 && count > int64(len(nodes)) && count > int64(preview.count) && count <= 1000000 {
				preview = albumPreview{count: int(count), ids: ids}
			}
		}
	}
	return preview
}

type albumResult struct {
	images   []imageCandidate
	complete bool
}

var albumScripts = regexp.MustCompile("(?is)<script\\b([^>]*)>(.*?)</script\\s*>")
var albumJSONType = regexp.MustCompile("(?i)(?:^|\\s)type\\s*=\\s*(?:\"application/json\"|'application/json'|application/json)(?:\\s|$)")

func readAlbumHTML(body []byte, postID string, preview albumPreview, secrets []string) albumResult {
	albumID := base64.StdEncoding.EncodeToString([]byte("mediaset:pcb." + postID))
	var best albumResult
	for _, script := range albumScripts.FindAllSubmatch(body, -1) {
		if !albumJSONType.Match(script[1]) || !bytes.Contains(script[2], []byte(albumID)) {
			continue
		}
		var data any
		decoder := json.NewDecoder(bytes.NewReader(script[2]))
		decoder.UseNumber()
		if decoder.Decode(&data) != nil {
			continue
		}
		visitAlbum(data, albumID, 0, func(media map[string]any) {
			result := albumImages(media, preview, secrets)
			if result.complete || (!best.complete && len(result.images) > len(best.images)) {
				best = result
			}
		})
	}
	return best
}

func visitAlbum(value any, albumID string, depth int, found func(map[string]any)) {
	if depth > 32 {
		return
	}
	switch node := value.(type) {
	case map[string]any:
		album, _ := node["album"].(map[string]any)
		if album["__typename"] == "MediaContainerMediaSet" && album["id"] == albumID {
			if media, ok := album["media"].(map[string]any); ok {
				found(media)
			}
		}
		for _, child := range node {
			visitAlbum(child, albumID, depth+1, found)
		}
	case []any:
		for _, child := range node {
			visitAlbum(child, albumID, depth+1, found)
		}
	}
}

func albumImages(media map[string]any, preview albumPreview, secrets []string) albumResult {
	edges, _ := media["edges"].([]any)
	page, _ := media["page_info"].(map[string]any)
	seen := make(map[string]bool, len(edges))
	result := albumResult{complete: page["has_next_page"] == false}
	for _, value := range edges {
		edge, _ := value.(map[string]any)
		node, _ := edge["node"].(map[string]any)
		id := scalarString(node["id"])
		kind := scalarString(node["__typename"])
		if !numericID.MatchString(id) || containsMediaSecret(id, secrets) || (kind != "Photo" && kind != "Video") {
			result.complete = false
			continue
		}
		seen[id] = true
		if kind != "Photo" {
			continue
		}
		image, _ := node["image"].(map[string]any)
		src := safeFBMediaURL(scalarString(image["uri"]))
		if src == "" || containsMediaSecret(src, secrets) {
			result.complete = false
			continue
		}
		result.images = append(result.images, imageCandidate{
			URL: src, Width: dimension(image["width"]), Height: dimension(image["height"]),
			Source: "attachment", Path: "album.media.edges.node.image.uri", PhotoID: id,
		})
	}
	for id := range preview.ids {
		if !seen[id] {
			result.complete = false
		}
	}
	result.complete = result.complete && len(seen) >= preview.count
	return result
}

func (t *captureTransport) readAlbum(ctx context.Context, postID string, preview albumPreview, secrets []string) (albumResult, error) {
	if t.albumHeaders == nil || !numericID.MatchString(postID) {
		return albumResult{}, nil
	}
	ctx, cancel := context.WithTimeout(ctx, 8*time.Second)
	defer cancel()
	target := "https://www.facebook.com/media/set/?set=pcb." + postID
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return albumResult{}, nil
	}
	req.Header = t.albumHeaders.Clone()
	// RoundTrip sends exactly one request and does not forward the session on redirects.
	resp, err := t.Base.RoundTrip(req)
	if err != nil {
		return albumResult{}, nil
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusTooManyRequests || resp.Header.Get("Retry-After") != "" {
		return albumResult{}, groups.ErrRateLimited
	}
	if resp.StatusCode >= 300 && resp.StatusCode < 400 {
		if location, err := url.Parse(resp.Header.Get("Location")); err == nil {
			switch strings.Split(strings.Trim(location.Path, "/"), "/")[0] {
			case "checkpoint":
				return albumResult{}, groups.ErrCheckpoint
			case "login", "login.php":
				return albumResult{}, groups.ErrSessionExpired
			}
		}
	}
	if resp.StatusCode != http.StatusOK {
		return albumResult{}, nil
	}
	const maxAlbumBody = 8 << 20
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxAlbumBody+1))
	if err != nil || len(body) > maxAlbumBody {
		return albumResult{}, nil
	}
	return readAlbumHTML(body, postID, preview, secrets), nil
}
