package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
)

var (
	rdb       *redis.Client
	resultTTL time.Duration
)

type QueuePayload struct {
	JobID          string `json:"job_id"`
	BorderColor    string `json:"border_color"`
	ColorPrecision int    `json:"color_precision"`
	Postprocess    bool   `json:"postprocess"`
}

func main() {
	redisURL := env("REDIS_URL", "redis://localhost:6379")
	ttlSec, _ := strconv.Atoi(env("RESULT_TTL", "3600"))
	resultTTL = time.Duration(ttlSec) * time.Second

	opt, err := redis.ParseURL(redisURL)
	if err != nil {
		log.Fatalf("bad REDIS_URL: %v", err)
	}
	rdb = redis.NewClient(opt)

	mux := http.NewServeMux()
	mux.HandleFunc("POST /patch", handleCreatePatch)
	mux.HandleFunc("GET /jobs/{job_id}", handleGetJob)
	mux.HandleFunc("GET /jobs/{job_id}/result", handleGetResult)
	mux.HandleFunc("GET /health", handleHealth)

	log.Println("listening on :8000")
	log.Fatal(http.ListenAndServe(":8000", mux))
}

func handleCreatePatch(w http.ResponseWriter, r *http.Request) {
	file, header, err := r.FormFile("file")
	if err != nil {
		http.Error(w, "missing file", http.StatusBadRequest)
		return
	}
	defer file.Close()

	inputBytes, err := io.ReadAll(file)
	if err != nil {
		http.Error(w, "failed to read file", http.StatusInternalServerError)
		return
	}

	filename := "input.png"
	if header.Filename != "" {
		filename = header.Filename
	}
	ext := "png"
	if i := strings.LastIndex(filename, "."); i >= 0 && i+1 < len(filename) {
		ext = filename[i+1:]
	}

	borderColor := r.FormValue("border_color")
	if borderColor == "" {
		borderColor = "#0a0a14"
	}

	colorPrecision := 8
	if v := r.FormValue("color_precision"); v != "" {
		colorPrecision, _ = strconv.Atoi(v)
	}

	postprocess := true
	if v := r.FormValue("postprocess"); v != "" {
		postprocess = v != "false" && v != "0" && v != "no"
	}

	jobID := uuid.New().String()
	ctx := context.Background()

	pipe := rdb.Pipeline()
	pipe.SetEx(ctx, fmt.Sprintf("job:%s:input", jobID), inputBytes, resultTTL)
	pipe.SetEx(ctx, fmt.Sprintf("job:%s:ext", jobID), ext, resultTTL)
	pipe.SetEx(ctx, fmt.Sprintf("job:%s:status", jobID), "processing", resultTTL)
	if _, err := pipe.Exec(ctx); err != nil {
		http.Error(w, "redis error", http.StatusInternalServerError)
		return
	}

	payload := QueuePayload{
		JobID:          jobID,
		BorderColor:    borderColor,
		ColorPrecision: colorPrecision,
		Postprocess:    postprocess,
	}
	payloadJSON, _ := json.Marshal(payload)

	if err := rdb.LPush(ctx, "patches", payloadJSON).Err(); err != nil {
		http.Error(w, "queue error", http.StatusInternalServerError)
		return
	}

	writeJSON(w, http.StatusOK, map[string]string{"job_id": jobID})
}

func handleGetJob(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("job_id")
	ctx := context.Background()

	status, err := rdb.Get(ctx, fmt.Sprintf("job:%s:status", jobID)).Result()
	if err == redis.Nil {
		http.Error(w, `{"detail":"job not found"}`, http.StatusNotFound)
		return
	} else if err != nil {
		http.Error(w, "redis error", http.StatusInternalServerError)
		return
	}

	resp := map[string]interface{}{"status": status}

	if status == "complete" {
		resp["result_url"] = fmt.Sprintf("/jobs/%s/result", jobID)
	} else if status == "failed" {
		errMsg, _ := rdb.Get(ctx, fmt.Sprintf("job:%s:error", jobID)).Result()
		if errMsg == "" {
			errMsg = "unknown error"
		}
		resp["error"] = errMsg
	}

	writeJSON(w, http.StatusOK, resp)
}

func handleGetResult(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("job_id")
	ctx := context.Background()

	data, err := rdb.Get(ctx, fmt.Sprintf("job:%s:result", jobID)).Bytes()
	if err == redis.Nil {
		http.Error(w, `{"detail":"result not ready"}`, http.StatusNotFound)
		return
	} else if err != nil {
		http.Error(w, "redis error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Content-Disposition", fmt.Sprintf(`inline; filename="%s.png"`, jobID))
	w.Write(data)
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	ctx := context.Background()
	if err := rdb.Ping(ctx).Err(); err != nil {
		http.Error(w, `{"detail":"redis unavailable"}`, http.StatusServiceUnavailable)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
