package conf

import (
	"os"
	"strconv"
	"time"
)

type Config struct {
	DatabasePath        string
	Bind                string
	SessionTTL          time.Duration
	DispatchLease       time.Duration
	GoogleDispatchLease time.Duration
	CardDispatchLease   time.Duration
	MaxDispatchCount    int
	CookieSecure        bool
	BootstrapUser       string
	BootstrapPass       string
	ClaudeCheckProxy    string
	ClaudeRegisterURL   string
	ChatGPTRedeemURL    string
	ChatGPTRedeemKey    string
}

func Load() Config {
	return Config{
		DatabasePath:        env("DATABASE_PATH", "./data/ccmax.db"),
		Bind:                env("BIND", ":4001"),
		SessionTTL:          time.Duration(envInt("SESSION_EXPIRE_HOURS", 24)) * time.Hour,
		DispatchLease:       time.Duration(envInt("ACCOUNT_DISPATCH_LEASE_MINUTES", 30)) * time.Minute,
		GoogleDispatchLease: time.Duration(envInt("GOOGLE_ACCOUNT_DISPATCH_LEASE_MINUTES", 3)) * time.Minute,
		CardDispatchLease:   time.Duration(envInt("CARD_DISPATCH_LEASE_MINUTES", 3)) * time.Minute,
		MaxDispatchCount:    envInt("MAX_DISPATCH_COUNT", 100),
		CookieSecure:        envBool("COOKIE_SECURE", false),
		BootstrapUser:       env("BOOTSTRAP_ADMIN_USERNAME", "admin"),
		BootstrapPass:       env("BOOTSTRAP_ADMIN_PASSWORD", ""),
		ClaudeCheckProxy:    env("CLAUDE_CHECK_PROXY", ""),
		ClaudeRegisterURL:   env("CLAUDE_REGISTER_BASE_URL", "http://claude-register:8000"),
		ChatGPTRedeemURL:    env("CHATGPT_REDEEM_BASE_URL", "https://example.com"),
		ChatGPTRedeemKey:    env("CHATGPT_REDEEM_API_KEY", ""),
	}
}

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func envInt(key string, fallback int) int {
	value, err := strconv.Atoi(os.Getenv(key))
	if err != nil || value <= 0 {
		return fallback
	}
	return value
}

func envBool(key string, fallback bool) bool {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return fallback
	}
	return parsed
}
