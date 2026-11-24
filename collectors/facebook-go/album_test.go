package main

import (
	"encoding/base64"
	"fmt"
	"testing"
)

func TestTruncatedMixedAlbum(t *testing.T) {
	story := decodeStory(t, `{"attachments":[{"styles":{
	  "__typename":"StoryAttachmentAlbumStyleRenderer",
	  "attachment":{"all_subattachments":{"count":3,"nodes":[
	    {"media":{"__typename":"Video","id":"10"}},
	    {"media":{"__typename":"Photo","id":"11","image":{"uri":"https://scontent.xx.fbcdn.net/preview.jpg","width":200,"height":200}}}
	  ]}}
	}}]}`)
	preview := incompleteAlbum(story)
	if preview.count != 3 || !preview.ids["10"] || !preview.ids["11"] {
		t.Fatalf("incorrect album preview: %+v", preview)
	}
	columnStory := decodeStory(t, `{"attachments":[{"styles":{
	  "__typename":"StoryAttachmentAlbumColumnStyleRenderer",
	  "attachment":{"all_subattachments":{"count":7,"nodes":[
	    {"media":{"__typename":"Photo","id":"11"}},
	    {"media":{"__typename":"Photo","id":"12"}},
	    {"media":{"__typename":"Photo","id":"13"}},
	    {"media":{"__typename":"Photo","id":"14"}},
	    {"media":{"__typename":"Photo","id":"15"}}
	  ]}}
	}}]}`)
	if column := incompleteAlbum(columnStory); column.count != 7 || len(column.ids) != 5 {
		t.Fatalf("incorrect column album preview: %+v", column)
	}
	albumID := base64.StdEncoding.EncodeToString([]byte("mediaset:pcb.123"))
	body := []byte(fmt.Sprintf(`<script type="application/json">{"require":[{"result":{"data":{"album":{
	  "__typename":"MediaContainerMediaSet","id":%q,"media":{"page_info":{"has_next_page":false},"edges":[
	    {"node":{"__typename":"Video","id":"10","image":{"uri":"https://scontent.xx.fbcdn.net/video-thumb.jpg"}}},
	    {"node":{"__typename":"Photo","id":"11","image":{"uri":"https://scontent.xx.fbcdn.net/same-photo.jpg","width":100,"height":100}}},
	    {"node":{"__typename":"Photo","id":"12","image":{"uri":"https://scontent.xx.fbcdn.net/missing.jpg?oe=AB&oh=signed%%2fvalue"}}}
	  ]}
	}}}}]}</script>`, albumID))
	result := readAlbumHTML(body, "123", preview, nil)
	images := deduplicateImages(append(extractImages(story), result.images...))
	if !result.complete || len(images) != 2 || images[0].PhotoID != "11" || images[1].PhotoID != "12" ||
		images[0].URL != "https://scontent.xx.fbcdn.net/preview.jpg" ||
		images[1].URL != "https://scontent.xx.fbcdn.net/missing.jpg?oe=AB&oh=signed%2fvalue" {
		t.Fatalf("incorrect gallery: complete=%t images=%+v", result.complete, images)
	}
	if wrong := readAlbumHTML(body, "456", preview, nil); wrong.complete || len(wrong.images) != 0 {
		t.Fatalf("accepted media from wrong album: %+v", wrong)
	}
	if blocked := readAlbumHTML(body, "123", preview, []string{"signed%2fvalue"}); blocked.complete || len(blocked.images) != 1 {
		t.Fatalf("accepted media with private session text: %+v", blocked)
	}
}
