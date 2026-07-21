package main

import (
	"flag"
	"log"
	"os"
	"runtime/debug"
	"strings"

	"ccmax/api"
	"ccmax/conf"
	"ccmax/dao"
	"ccmax/web"

	"github.com/gin-gonic/gin"
)

func main() {
	cfg := conf.Load()
	bind := flag.String("bind", cfg.Bind, "HTTP bind address")
	_ = flag.String("app-env", os.Getenv("APP_ENV"), "application environment")
	flag.Parse()
	cfg.Bind = *bind
	store, err := dao.Open(cfg.DatabasePath)
	if err != nil {
		log.Fatal(err)
	}
	defer store.Close()
	server := api.New(store, cfg)
	if err = server.Bootstrap(); err != nil {
		log.Fatal(err)
	}
	if os.Getenv("APP_ENV") != "dev" {
		gin.SetMode(gin.ReleaseMode)
	}
	r := gin.New()
	_ = r.SetTrustedProxies(nil)
	r.Use(gin.Recovery(), gin.Logger())
	server.Setup(r)
	if err = web.SetupSPA(r); err != nil {
		log.Fatal(err)
	}
	log.Printf("ccmax %s listening on %s", buildVersion(), cfg.Bind)
	if err = r.Run(cfg.Bind); err != nil {
		log.Fatal(err)
	}
}

func buildVersion() string {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return "dev"
	}
	values := make(map[string]string, len(info.Settings))
	for _, setting := range info.Settings {
		values[setting.Key] = setting.Value
	}
	revision := values["vcs.revision"]
	if revision == "" {
		if info.Main.Version != "" && info.Main.Version != "(devel)" {
			return info.Main.Version
		}
		return "dev"
	}
	if len(revision) > 12 {
		revision = revision[:12]
	}
	if strings.EqualFold(values["vcs.modified"], "true") {
		revision += "-dirty"
	}
	if committedAt := values["vcs.time"]; committedAt != "" {
		return revision + " (" + committedAt + ")"
	}
	return revision
}
