package web

import "embed"

// Dist contains the production Vite SPA.
//
//go:embed all:dist
var Dist embed.FS
