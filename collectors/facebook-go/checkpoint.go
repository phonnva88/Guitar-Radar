package main

import (
	"encoding/json"
	"errors"
	"io"
	"os"
	"time"
)

const checkpointLimit = 4 * 1024 * 1024

// Checkpoints go through private files. The public collector envelope and logs
// never contain Facebook's opaque pagination cursors.
type checkpoint struct {
	Group        string          `json:"group"`
	Account      string          `json:"account"`
	From         time.Time       `json:"from"`
	To           time.Time       `json:"to"`
	PageSize     int             `json:"page_size"`
	Cursor       string          `json:"cursor"`
	SeenCursors  map[string]bool `json:"seen_cursors"`
	SeenPosts    map[string]bool `json:"seen_posts"`
	PreviousTime time.Time       `json:"previous_time"`
	Unordered    bool            `json:"unordered"`
}

func readCheckpoint(path string, cfg config, group, account string) (*checkpoint, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, checkpointLimit+1))
	if err != nil {
		return nil, err
	}
	var state checkpoint
	if len(data) > checkpointLimit || json.Unmarshal(data, &state) != nil ||
		state.Cursor == "" || !state.SeenCursors[state.Cursor] ||
		state.Group != group || state.Account != account || state.PageSize != cfg.PageSize ||
		!state.From.Equal(cfg.From) || !state.To.Equal(cfg.To) {
		return nil, errors.New("checkpoint does not match the collection")
	}
	if state.SeenPosts == nil {
		state.SeenPosts = map[string]bool{}
	}
	return &state, nil
}
