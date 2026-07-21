package web

import (
	"io/fs"
	"mime"
	"net/http"
	"path"
	"strings"

	"github.com/gin-gonic/gin"
)

func SetupSPA(router *gin.Engine) error {
	dist, err := fs.Sub(Dist, "dist")
	if err != nil {
		return err
	}
	index, err := fs.ReadFile(dist, "index.html")
	if err != nil {
		return err
	}
	router.NoRoute(func(c *gin.Context) {
		if strings.HasPrefix(c.Request.URL.Path, "/api/") {
			c.JSON(http.StatusNotFound, gin.H{"code": "NOT_FOUND", "message": "接口不存在"})
			return
		}
		name := strings.TrimPrefix(path.Clean(c.Request.URL.Path), "/")
		if name != "" && name != "." {
			if data, readErr := fs.ReadFile(dist, name); readErr == nil {
				if kind := mime.TypeByExtension(path.Ext(name)); kind != "" {
					c.Header("Content-Type", kind)
				}
				c.Data(http.StatusOK, c.Writer.Header().Get("Content-Type"), data)
				return
			}
		}
		c.Data(http.StatusOK, "text/html; charset=utf-8", index)
	})
	return nil
}
