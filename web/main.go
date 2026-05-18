package main

import (
	"context"
	"crypto/rand"
	"database/sql"
	"embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	picdb "pic-to-patch/db"

	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	"golang.org/x/oauth2"
)

//go:embed index.html
var frontend embed.FS

var (
	rdb          *redis.Client
	db           *sql.DB
	resultTTL    time.Duration
	storageDir   string
	oidcVerifier *oidc.IDTokenVerifier
	oauth2Config *oauth2.Config
	sessionTTL   = 7 * 24 * time.Hour
	cookieName   = "p2p_session"
)

var corsAllowedRe = regexp.MustCompile(`^https?://([a-zA-Z0-9-]+\.)*matmanna\.dev(:[0-9]+)?$`)

func corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		if origin != "" && corsAllowedRe.MatchString(origin) {
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Set("Vary", "Origin")
			w.Header().Set("Access-Control-Allow-Credentials", "true")
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
			if reqHeaders := r.Header.Get("Access-Control-Request-Headers"); reqHeaders != "" {
				w.Header().Set("Access-Control-Allow-Headers", reqHeaders)
			} else {
				w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
			}
			if r.Method == http.MethodOptions {
				w.WriteHeader(http.StatusNoContent)
				return
			}
		}
		next.ServeHTTP(w, r)
	})
}

type QueuePayload struct {
	JobID           string `json:"job_id"`
	BorderColor     string `json:"border_color"`
	ColorPrecision  int    `json:"color_precision"`
	Postprocess     bool   `json:"postprocess"`
	BackgroundColor string `json:"background_color,omitempty"`
	PatchShape      string `json:"patch_shape,omitempty"`
	OutputSize      int    `json:"output_size,omitempty"`
	StitchDensity   int    `json:"stitch_density,omitempty"`
	InputPath       string `json:"input_path"`
}

func main() {
	redisURL := env("REDIS_URL", "redis://localhost:6379")
	dbPath := env("DATABASE_PATH", "/data/pic-to-patch.db")
	storageDir = env("STORAGE_DIR", "/data/images")
	baseURL := env("BASE_URL", "http://localhost:8000")
	hcClientID := env("HACKCLUB_CLIENT_ID", "")
	hcClientSecret := env("HACKCLUB_CLIENT_SECRET", "")
	ttlSec, _ := strconv.Atoi(env("RESULT_TTL", "3600"))
	resultTTL = time.Duration(ttlSec) * time.Second

	opt, err := redis.ParseURL(redisURL)
	if err != nil {
		log.Fatalf("bad REDIS_URL: %v", err)
	}
	rdb = redis.NewClient(opt)

	db, err = picdb.Open(dbPath)
	if err != nil {
		log.Fatalf("db open: %v", err)
	}
	if err := picdb.Migrate(db); err != nil {
		log.Fatalf("db migrate: %v", err)
	}

	if err := os.MkdirAll(storageDir, 0755); err != nil {
		log.Fatalf("storage dir: %v", err)
	}

	if hcClientID != "" && hcClientSecret != "" {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		provider, err := oidc.NewProvider(ctx, "https://auth.hackclub.com")
		if err != nil {
			log.Printf("OIDC auth disabled (provider unavailable): %v", err)
		} else {
			oauth2Config = &oauth2.Config{
				ClientID:     hcClientID,
				ClientSecret: hcClientSecret,
				RedirectURL:  baseURL + "/auth/callback",
				Endpoint:     provider.Endpoint(),
				Scopes:       []string{oidc.ScopeOpenID, "profile", "email", "verification_status"},
			}
			oidcVerifier = provider.Verifier(&oidc.Config{ClientID: hcClientID})
			log.Println("OIDC auth enabled")
		}
	} else {
		log.Println("OIDC auth disabled (set HACKCLUB_CLIENT_ID and HACKCLUB_CLIENT_SECRET)")
	}

	mux := http.NewServeMux()
	mux.HandleFunc("POST /patch", handleCreatePatch)
	mux.HandleFunc("GET /patch/{patch_id}", handleGetPatch)
	mux.HandleFunc("GET /jobs/{job_id}", handleGetJob)
	mux.HandleFunc("POST /jobs/{job_id}/publish", handlePublishJob)
	mux.HandleFunc("GET /jobs/{job_id}/result", handleGetResult)
	mux.HandleFunc("GET /jobs/{job_id}/original", handleGetOriginal)
	mux.HandleFunc("GET /jobs", handleListJobs)
	mux.HandleFunc("DELETE /jobs/{job_id}", handleDeleteJob)
	mux.HandleFunc("GET /auth/login", handleAuthLogin)
	mux.HandleFunc("GET /auth/callback", handleAuthCallback)
	mux.HandleFunc("GET /auth/logout", handleAuthLogout)
	mux.HandleFunc("GET /auth/me", handleAuthMe)
	mux.HandleFunc("GET /patches", handleRoot)
	mux.HandleFunc("POST /patch/{patch_id}/star", handleStar)
	mux.HandleFunc("DELETE /patch/{patch_id}/star", handleUnstar)
	mux.HandleFunc("GET /stars", handleStars)
	mux.HandleFunc("GET /my-patches", handleMyPatches)
	mux.HandleFunc("GET /health", handleHealth)
	mux.HandleFunc("GET /", handleRoot)

	log.Println("listening on :8000")
	log.Fatal(http.ListenAndServe(":8000", corsMiddleware(mux)))
}

func clientIP(r *http.Request) string {
	if fwd := r.Header.Get("X-Forwarded-For"); fwd != "" {
		if i := strings.Index(fwd, ","); i >= 0 {
			return strings.TrimSpace(fwd[:i])
		}
		return strings.TrimSpace(fwd)
	}
	if addr := r.Header.Get("X-Real-IP"); addr != "" {
		return addr
	}
	if i := strings.LastIndex(r.RemoteAddr, ":"); i >= 0 {
		return r.RemoteAddr[:i]
	}
	return r.RemoteAddr
}

func sessionUser(r *http.Request) (string, map[string]interface{}) {
	c, err := r.Cookie(cookieName)
	if err != nil {
		return "", nil
	}
	s, err := picdb.GetSession(db, c.Value)
	if err != nil {
		return "", nil
	}
	return s.UserSub, s.UserData
}

func genToken() string {
	b := make([]byte, 32)
	rand.Read(b)
	return hex.EncodeToString(b)
}

func randState() string {
	b := make([]byte, 16)
	rand.Read(b)
	return hex.EncodeToString(b)
}

func setCookie(w http.ResponseWriter, name, value string, maxAge int) {
	http.SetCookie(w, &http.Cookie{
		Name:     name,
		Value:    value,
		Path:     "/",
		MaxAge:   maxAge,
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
	})
}

// ---------------------------------------------------------------------------
// Auth handlers
// ---------------------------------------------------------------------------

func handleAuthLogin(w http.ResponseWriter, r *http.Request) {
	if oauth2Config == nil {
		http.Error(w, "auth not configured", http.StatusServiceUnavailable)
		return
	}
	state := randState()
	setCookie(w, "oauth_state", state, 600)
	url := oauth2Config.AuthCodeURL(state)
	http.Redirect(w, r, url, http.StatusFound)
}

func handleAuthCallback(w http.ResponseWriter, r *http.Request) {
	if oauth2Config == nil || oidcVerifier == nil {
		http.Error(w, "auth not configured", http.StatusServiceUnavailable)
		return
	}

	stateCookie, err := r.Cookie("oauth_state")
	if err != nil || stateCookie.Value == "" {
		http.Error(w, "missing state", http.StatusBadRequest)
		return
	}
	setCookie(w, "oauth_state", "", -1)

	if r.URL.Query().Get("state") != stateCookie.Value {
		http.Error(w, "state mismatch", http.StatusBadRequest)
		return
	}

	code := r.URL.Query().Get("code")
	if code == "" {
		http.Error(w, "missing code", http.StatusBadRequest)
		return
	}

	ctx := context.Background()
	token, err := oauth2Config.Exchange(ctx, code)
	if err != nil {
		log.Printf("token exchange: %v", err)
		http.Error(w, "token exchange failed", http.StatusInternalServerError)
		return
	}

	rawIDToken, ok := token.Extra("id_token").(string)
	if !ok {
		http.Error(w, "no id_token", http.StatusInternalServerError)
		return
	}

	idToken, err := oidcVerifier.Verify(ctx, rawIDToken)
	if err != nil {
		log.Printf("id_token verify: %v", err)
		http.Error(w, "invalid id_token", http.StatusInternalServerError)
		return
	}

	var claims map[string]interface{}
	if err := idToken.Claims(&claims); err != nil {
		http.Error(w, "bad claims", http.StatusInternalServerError)
		return
	}

	userSub := idToken.Subject

	if name, _ := claims["name"].(string); name != "" {
		if email, _ := claims["email"].(string); email != "" {
			picdb.UpsertUser(db, userSub, email, name)
		}
	}

	sessionToken := genToken()
	if err := picdb.CreateSession(db, sessionToken, userSub, claims, sessionTTL); err != nil {
		log.Printf("create session: %v", err)
		http.Error(w, "session error", http.StatusInternalServerError)
		return
	}

	setCookie(w, cookieName, sessionToken, int(sessionTTL.Seconds()))
	http.Redirect(w, r, "/", http.StatusFound)
}

func handleAuthLogout(w http.ResponseWriter, r *http.Request) {
	c, err := r.Cookie(cookieName)
	if err == nil && c.Value != "" {
		picdb.DeleteSession(db, c.Value)
	}
	setCookie(w, cookieName, "", -1)
	writeJSON(w, http.StatusOK, map[string]string{"status": "logged_out"})
}

func handleAuthMe(w http.ResponseWriter, r *http.Request) {
	sub, data := sessionUser(r)
	if sub == "" {
		writeJSON(w, http.StatusOK, map[string]interface{}{"authenticated": false})
		return
	}
	resp := map[string]interface{}{
		"authenticated": true,
		"sub":           sub,
		"name":          data["name"],
	}
	if nick, ok := data["nickname"]; ok {
		resp["nickname"] = nick
	}
	writeJSON(w, http.StatusOK, resp)
}

// ---------------------------------------------------------------------------
// Patch handlers
// ---------------------------------------------------------------------------

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

	backgroundColor := r.FormValue("background_color")
	patchShape := r.FormValue("patch_shape")

	outputSize := 0
	if v := r.FormValue("output_size"); v != "" {
		outputSize, _ = strconv.Atoi(v)
	}

	stitchDensity := 0
	if v := r.FormValue("stitch_density"); v != "" {
		stitchDensity, _ = strconv.Atoi(v)
	}

	name := r.FormValue("name")
	description := r.FormValue("description")

	jobID := uuid.New().String()
	ip := clientIP(r)
	userSub, _ := sessionUser(r)

	if userSub == "" {
		now := time.Now().UTC()
		monthStart := time.Date(now.Year(), now.Month(), 1, 0, 0, 0, 0, time.UTC)
		count, err := picdb.PatchesByIP(db, ip, monthStart)
		if err != nil {
			log.Printf("rate check error: %v", err)
		}
		if count >= 10 {
			http.Error(w, `{"error":"rate limit exceeded","detail":"10 generations per month for signed-out users. Sign in with Hack Club for unlimited usage."}`, http.StatusTooManyRequests)
			return
		}
	}

	originalPath := filepath.Join(storageDir, jobID+"_input."+ext)
	if err := os.WriteFile(originalPath, inputBytes, 0644); err != nil {
		http.Error(w, "failed to save file", http.StatusInternalServerError)
		return
	}

	tx, err := db.Begin()
	if err != nil {
		http.Error(w, "db error", http.StatusInternalServerError)
		return
	}
	defer tx.Rollback()

	if err := picdb.CreatePatch(tx, picdb.CreatePatchParams{
		ID:               jobID,
		UserID:           userSub,
		IPAddress:        ip,
		OriginalFilename: filename,
		OriginalPath:     originalPath,
		BorderColor:      borderColor,
		ColorPrecision:   colorPrecision,
		Postprocess:      postprocess,
		BackgroundColor:  backgroundColor,
		PatchShape:       patchShape,
		OutputSize:       outputSize,
		StitchDensity:    stitchDensity,
		Name:             name,
		Description:      description,
	}); err != nil {
		http.Error(w, "db error", http.StatusInternalServerError)
		return
	}

	if err := tx.Commit(); err != nil {
		http.Error(w, "db error", http.StatusInternalServerError)
		return
	}

	ctx := context.Background()
	pipe := rdb.Pipeline()
	pipe.SetEx(ctx, fmt.Sprintf("job:%s:status", jobID), "processing", resultTTL)
	if _, err := pipe.Exec(ctx); err != nil {
		log.Printf("redis error for %s: %v", jobID, err)
	}

	payload := QueuePayload{
		JobID:           jobID,
		BorderColor:     borderColor,
		ColorPrecision:  colorPrecision,
		Postprocess:     postprocess,
		BackgroundColor: backgroundColor,
		PatchShape:      patchShape,
		OutputSize:      outputSize,
		StitchDensity:   stitchDensity,
		InputPath:       originalPath,
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

	p, err := picdb.GetPatch(db, jobID)
	if err != nil {
		http.Error(w, `{"detail":"job not found"}`, http.StatusNotFound)
		return
	}

	userSub, _ := sessionUser(r)
	if !canViewPatch(p, userSub) {
		http.NotFound(w, r)
		return
	}
	if wantsHTML(r) {
		serveFrontend(w)
		return
	}
	writeJSON(w, http.StatusOK, enrichPatch(p, userSub))
}

func enrichPatch(p *picdb.Patch, userSub string) map[string]interface{} {
	resp := map[string]interface{}{
		"id":                p.ID,
		"status":            p.Status,
		"original_filename": p.OriginalFilename,
		"border_color":      p.BorderColor,
		"color_precision":   p.ColorPrecision,
		"postprocess":       p.Postprocess,
		"created_at":        p.CreatedAt,
		"name":              p.Name,
		"description":       p.Description,
	}
	if p.BackgroundColor != "" {
		resp["background_color"] = p.BackgroundColor
	}
	if p.PatchShape != "" {
		resp["patch_shape"] = p.PatchShape
	}
	if p.OutputSize > 0 {
		resp["output_size"] = p.OutputSize
	}
	if p.StitchDensity > 0 {
		resp["stitch_density"] = p.StitchDensity
	}
	if p.UserID != "" {
		resp["user_name"] = picdb.UserName(db, p.UserID)
		resp["user_id"] = p.UserID
	}
	if p.Status == "complete" {
		resp["result_url"] = p.ResultURL()
		resp["original_url"] = p.OriginalURL()
	} else if p.Status == "failed" {
		resp["error"] = p.ErrorMessage
	}
	if starCount, err := picdb.StarCount(db, p.ID); err == nil {
		resp["star_count"] = starCount
	}
	if userSub != "" {
		if starred, err := picdb.UserStarred(db, p.ID, userSub); err == nil {
			resp["user_starred"] = starred
		}
	}
	return resp
}

func handleGetPatch(w http.ResponseWriter, r *http.Request) {
	patchID := r.PathValue("patch_id")

	p, err := picdb.GetPatch(db, patchID)
	if err != nil {
		http.Error(w, `{"detail":"patch not found"}`, http.StatusNotFound)
		return
	}

	userSub, _ := sessionUser(r)
	if !canViewPatch(p, userSub) {
		http.NotFound(w, r)
		return
	}
	writeJSON(w, http.StatusOK, enrichPatch(p, userSub))
}

func handleGetResult(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("job_id")

	p, err := picdb.GetPatch(db, jobID)
	if err != nil {
		http.Error(w, `{"detail":"job not found"}`, http.StatusNotFound)
		return
	}
	userSub, _ := sessionUser(r)
	if !canViewPatch(p, userSub) {
		http.NotFound(w, r)
		return
	}

	if p.ResultPath == "" {
		http.Error(w, `{"detail":"result not ready"}`, http.StatusNotFound)
		return
	}

	data, err := os.ReadFile(p.ResultPath)
	if err != nil {
		http.Error(w, `{"detail":"result file missing"}`, http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Content-Disposition", fmt.Sprintf(`inline; filename="%s.png"`, jobID))
	w.Write(data)
}

func handleGetOriginal(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("job_id")

	p, err := picdb.GetPatch(db, jobID)
	if err != nil {
		http.Error(w, `{"detail":"job not found"}`, http.StatusNotFound)
		return
	}
	userSub, _ := sessionUser(r)
	if !canViewPatch(p, userSub) {
		http.NotFound(w, r)
		return
	}

	if p.OriginalPath == "" {
		http.Error(w, `{"detail":"original file missing"}`, http.StatusNotFound)
		return
	}

	data, err := os.ReadFile(p.OriginalPath)
	if err != nil {
		http.Error(w, `{"detail":"original file missing"}`, http.StatusNotFound)
		return
	}

	ext := strings.TrimPrefix(filepath.Ext(p.OriginalPath), ".")
	mime := "image/" + ext
	if ext == "svg" {
		mime = "image/svg+xml"
	}

	w.Header().Set("Content-Type", mime)
	w.Header().Set("Content-Disposition", fmt.Sprintf(`inline; filename="%s"`, p.OriginalFilename))
	w.Write(data)
}

func handleListJobs(w http.ResponseWriter, r *http.Request) {
	patches, err := picdb.RecentPatches(db, 20)
	if err != nil {
		http.Error(w, `{"detail":"db error"}`, http.StatusInternalServerError)
		return
	}

	userSub, _ := sessionUser(r)
	results := make([]map[string]interface{}, 0, len(patches))
	for _, p := range patches {
		results = append(results, enrichPatch(&p, userSub))
	}

	writeJSON(w, http.StatusOK, results)
}

func handlePublishJob(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("job_id")
	p, err := picdb.GetPatch(db, jobID)
	if err != nil {
		http.Error(w, `{"detail":"job not found"}`, http.StatusNotFound)
		return
	}

	userSub, _ := sessionUser(r)
	if p.UserID != "" && p.UserID != userSub {
		http.Error(w, `{"error":"not allowed"}`, http.StatusForbidden)
		return
	}

	if err := r.ParseForm(); err != nil {
		http.Error(w, `{"error":"bad form"}`, http.StatusBadRequest)
		return
	}

	name := strings.TrimSpace(r.FormValue("name"))
	description := strings.TrimSpace(r.FormValue("description"))
	isPublic := r.FormValue("is_public") == "1" || r.FormValue("is_public") == "true"
	if isPublic && name == "" {
		http.Error(w, `{"error":"name required for public patches"}`, http.StatusBadRequest)
		return
	}
	if isPublic {
		if err := picdb.PublishPatch(db, jobID, name, description); err != nil {
			log.Printf("publish patch %s: %v", jobID, err)
			http.Error(w, `{"error":"publish failed"}`, http.StatusInternalServerError)
			return
		}
	} else {
		// saving unlisted -> update name/description without publishing
		if name != "" || description != "" {
			if err := picdb.PublishPatch(db, jobID, name, description); err != nil {
				log.Printf("save unlisted patch %s: %v", jobID, err)
				http.Error(w, `{"error":"save failed"}`, http.StatusInternalServerError)
				return
			}
		}
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"status": func() string {
			if isPublic {
				return "published"
			}
			return "saved"
		}(),
		"share_url":   "/jobs/" + jobID,
		"job_id":      jobID,
		"name":        name,
		"description": description,
	})
}

func canViewPatch(p *picdb.Patch, userSub string) bool {
	return p.IsPublic || (userSub != "" && p.UserID == userSub)
}

func wantsHTML(r *http.Request) bool {
	return strings.Contains(r.Header.Get("Accept"), "text/html")
}

func handleDeleteJob(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("job_id")
	p, err := picdb.GetPatch(db, jobID)
	if err != nil {
		http.Error(w, `{"detail":"job not found"}`, http.StatusNotFound)
		return
	}
	userSub, _ := sessionUser(r)
	if p.UserID != "" && p.UserID != userSub {
		http.Error(w, `{"error":"not allowed"}`, http.StatusForbidden)
		return
	}

	// remove files if present
	if p.ResultPath != "" {
		os.Remove(p.ResultPath)
	}
	if p.OriginalPath != "" {
		os.Remove(p.OriginalPath)
	}

	if err := picdb.DeletePatch(db, jobID); err != nil {
		log.Printf("delete patch %s: %v", jobID, err)
		http.Error(w, `{"error":"delete failed"}`, http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"status": "deleted", "job_id": jobID})
}

// ---------------------------------------------------------------------------
// Star handlers
// ---------------------------------------------------------------------------

func handleStar(w http.ResponseWriter, r *http.Request) {
	patchID := r.PathValue("patch_id")
	userSub, _ := sessionUser(r)
	if userSub == "" {
		http.Error(w, `{"error":"authentication required"}`, http.StatusUnauthorized)
		return
	}
	if err := picdb.AddStar(db, patchID, userSub); err != nil {
		http.Error(w, `{"error":"star failed"}`, http.StatusInternalServerError)
		return
	}
	count, _ := picdb.StarCount(db, patchID)
	writeJSON(w, http.StatusOK, map[string]interface{}{"starred": true, "star_count": count})
}

func handleUnstar(w http.ResponseWriter, r *http.Request) {
	patchID := r.PathValue("patch_id")
	userSub, _ := sessionUser(r)
	if userSub == "" {
		http.Error(w, `{"error":"authentication required"}`, http.StatusUnauthorized)
		return
	}
	if err := picdb.RemoveStar(db, patchID, userSub); err != nil {
		http.Error(w, `{"error":"unstar failed"}`, http.StatusInternalServerError)
		return
	}
	count, _ := picdb.StarCount(db, patchID)
	writeJSON(w, http.StatusOK, map[string]interface{}{"starred": false, "star_count": count})
}

func handleStars(w http.ResponseWriter, r *http.Request) {
	userSub, _ := sessionUser(r)
	if userSub == "" {
		http.Error(w, `{"error":"authentication required"}`, http.StatusUnauthorized)
		return
	}
	ids, err := picdb.UserStars(db, userSub)
	if err != nil {
		http.Error(w, `{"error":"db error"}`, http.StatusInternalServerError)
		return
	}
	patches := make([]map[string]interface{}, 0, len(ids))
	for _, id := range ids {
		p, err := picdb.GetPatch(db, id)
		if err != nil {
			continue
		}
		patches = append(patches, enrichPatch(p, userSub))
	}
	writeJSON(w, http.StatusOK, patches)
}

func handleMyPatches(w http.ResponseWriter, r *http.Request) {
	userSub, _ := sessionUser(r)
	if userSub == "" {
		http.Error(w, `{"error":"authentication required"}`, http.StatusUnauthorized)
		return
	}
	patches, err := picdb.PatchesByUser(db, userSub, 50)
	if err != nil {
		http.Error(w, `{"detail":"db error"}`, http.StatusInternalServerError)
		return
	}
	results := make([]map[string]interface{}, 0, len(patches))
	for _, p := range patches {
		results = append(results, enrichPatch(&p, userSub))
	}
	writeJSON(w, http.StatusOK, results)
}

func handleRoot(w http.ResponseWriter, r *http.Request) {
	// Serve the frontend HTML for root and SPA routes like /browse and /my-patches-page.
	if r.URL.Path != "/" && r.URL.Path != "/browse" && r.URL.Path != "/patches" && r.URL.Path != "/my-patches-page" {
		http.NotFound(w, r)
		return
	}
	serveFrontend(w)
}

func serveFrontend(w http.ResponseWriter) {
	data, err := frontend.ReadFile("index.html")
	if err != nil {
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write(data)
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	ctx := context.Background()
	if err := rdb.Ping(ctx).Err(); err != nil {
		http.Error(w, `{"detail":"redis unavailable"}`, http.StatusServiceUnavailable)
		return
	}
	if err := db.Ping(); err != nil {
		http.Error(w, `{"detail":"db unavailable"}`, http.StatusServiceUnavailable)
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