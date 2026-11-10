package main

import (
	"net/url"
	"path"
	"regexp"
	"strings"
)

// Match only Facebook photo object names, not arbitrary CDN endpoints whose
// query parameters may select entirely different images.
var facebookPhotoObject = regexp.MustCompile(`(?i)^[0-9]+_[0-9]+_[0-9]+_[no]\.(?:jpe?g|png|webp|avif)$`)

func imageIdentity(image imageCandidate, local bool) string {
	if image.PhotoID != "" {
		return "photo:" + image.PhotoID
	}
	if local && image.photoScope != "" {
		return "scope:" + image.photoScope
	}
	if u, err := url.Parse(image.URL); err == nil {
		host, name := strings.ToLower(u.Hostname()), path.Base(u.Path)
		if (host == "fbcdn.net" || strings.HasSuffix(host, ".fbcdn.net")) && facebookPhotoObject.MatchString(name) {
			return "fbcdn:" + strings.ToLower(name)
		}
	}
	return "url:" + image.URL
}

func imageArea(image imageCandidate) float64 {
	// Dimensions are vetted int64 values. Floats avoid overflowing their product.
	w, wok := image.Width.(int64)
	h, hok := image.Height.(int64)
	if !wok || !hok || w <= 0 || h <= 0 {
		return 0
	}
	return float64(w) * float64(h)
}

func deduplicateImages(images []imageCandidate) []imageCandidate {
	merge := func(input []imageCandidate, local bool) []imageCandidate {
		result := []imageCandidate{}
		positions := map[string]int{}
		for _, image := range input {
			key := imageIdentity(image, local)
			if position, exists := positions[key]; exists {
				if imageArea(image) > imageArea(result[position]) {
					result[position] = image
				}
			} else {
				positions[key] = len(result)
				result = append(result, image)
			}
		}
		return result
	}
	// First collapse rendition fields of a Photo without an ID, then deduplicate
	// repeated photo nodes/responses. Keep the complete chosen URL for fetching.
	result := merge(merge(images, true), false)
	for i := range result {
		result[i].photoScope = ""
	}
	return result
}
