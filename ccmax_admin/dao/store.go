package dao

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

var ErrNotFound = sql.ErrNoRows

type Store struct{ DB *sql.DB }

func Open(path string) (*Store, error) {
	if path == "" {
		return nil, errors.New("database path is required")
	}
	if path != ":memory:" && !strings.HasPrefix(path, "file:") {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return nil, fmt.Errorf("create database directory: %w", err)
		}
	}
	dsn := path
	if path == ":memory:" {
		dsn = "file:ccmax-memory?mode=memory&cache=shared"
	} else if !strings.HasPrefix(path, "file:") {
		dsn = "file:" + path
	}
	separator := "?"
	if strings.Contains(dsn, "?") {
		separator = "&"
	}
	dsn += separator + "_pragma=foreign_keys(1)&_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_txlock=immediate"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// One writer connection makes account allocation deterministic and avoids
	// SQLITE_BUSY races while still allowing WAL readers in external tools.
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	if err := db.Ping(); err != nil {
		db.Close()
		return nil, err
	}
	store := &Store{DB: db}
	if err := store.Migrate(context.Background()); err != nil {
		db.Close()
		return nil, err
	}
	return store, nil
}

func (s *Store) Close() error { return s.DB.Close() }

func (s *Store) Migrate(ctx context.Context) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)`,
		`CREATE TABLE IF NOT EXISTS admin_users (
			id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
			password_hash TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
			role TEXT NOT NULL DEFAULT 'admin' CHECK(role IN ('super_admin','admin')),
			status INTEGER NOT NULL DEFAULT 1 CHECK(status IN (1,-1)), last_login_at DATETIME,
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)`,
		`CREATE TABLE IF NOT EXISTS admin_sessions (
			id INTEGER PRIMARY KEY AUTOINCREMENT, admin_user_id INTEGER NOT NULL,
			token_hash TEXT NOT NULL UNIQUE, ip TEXT NOT NULL DEFAULT '', user_agent TEXT NOT NULL DEFAULT '',
			expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			FOREIGN KEY(admin_user_id) REFERENCES admin_users(id) ON DELETE CASCADE)`,
		`CREATE INDEX IF NOT EXISTS idx_admin_sessions_expire ON admin_sessions(expires_at)`,
		`CREATE TABLE IF NOT EXISTS claude_accounts (
			id INTEGER PRIMARY KEY AUTOINCREMENT, mail TEXT NOT NULL COLLATE NOCASE UNIQUE,
			password TEXT NOT NULL, session_key TEXT NOT NULL UNIQUE,
			plan TEXT NOT NULL DEFAULT 'free' CHECK(plan IN ('free','max_20x')),
			status INTEGER NOT NULL DEFAULT 1 CHECK(status IN (1,-1)),
			last_dispatched_at DATETIME, dispatch_count INTEGER NOT NULL DEFAULT 0,
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, upgraded_at DATETIME,
			updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)`,
		`CREATE INDEX IF NOT EXISTS idx_claude_dispatch ON claude_accounts(plan,status,last_dispatched_at)`,
		`CREATE TABLE IF NOT EXISTS api_keys (
			id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, key_prefix TEXT NOT NULL,
			key_hash TEXT NOT NULL UNIQUE, status INTEGER NOT NULL DEFAULT 1 CHECK(status IN (1,-1)),
			last_used_at DATETIME, expires_at DATETIME, created_by INTEGER NOT NULL,
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			FOREIGN KEY(created_by) REFERENCES admin_users(id))`,
		`CREATE TABLE IF NOT EXISTS claude_account_dispatches (
			id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, api_key_id INTEGER NOT NULL,
			account_id INTEGER NOT NULL, requested_plan TEXT NOT NULL, client_ip TEXT NOT NULL DEFAULT '',
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			UNIQUE(request_id,account_id), FOREIGN KEY(api_key_id) REFERENCES api_keys(id),
			FOREIGN KEY(account_id) REFERENCES claude_accounts(id))`,
		`CREATE INDEX IF NOT EXISTS idx_dispatch_request ON claude_account_dispatches(request_id,api_key_id)`,
		`CREATE TABLE IF NOT EXISTS card_pool (
			id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, card_id TEXT NOT NULL DEFAULT '',
			card_no TEXT NOT NULL, expire_mmyy TEXT NOT NULL CHECK(length(expire_mmyy)=4),
			ccv TEXT NOT NULL CHECK(length(ccv) BETWEEN 3 AND 4), usage_count INTEGER NOT NULL DEFAULT 0,
			status INTEGER NOT NULL DEFAULT 1 CHECK(status IN (1,-1)),
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			UNIQUE(card_no,expire_mmyy,ccv,source))`,
		`CREATE INDEX IF NOT EXISTS idx_card_pool_source ON card_pool(source)`,
		`CREATE INDEX IF NOT EXISTS idx_card_pool_status ON card_pool(status)`,
		`CREATE TABLE IF NOT EXISTS channel_credentials (
			id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL COLLATE NOCASE UNIQUE,
			token TEXT NOT NULL, status INTEGER NOT NULL DEFAULT 1 CHECK(status IN (1,-1)),
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)`,
		`CREATE TABLE IF NOT EXISTS orders (
			id INTEGER PRIMARY KEY AUTOINCREMENT, batch_no TEXT NOT NULL UNIQUE, buyer TEXT NOT NULL,
			sale_price_cents INTEGER NOT NULL DEFAULT 0, quantity INTEGER NOT NULL CHECK(quantity>0),
			plan TEXT NOT NULL CHECK(plan IN ('free','max_20x')),
			status TEXT NOT NULL DEFAULT 'created' CHECK(status IN ('created','allocated','cancelled')),
			remark TEXT NOT NULL DEFAULT '', created_by INTEGER NOT NULL, allocated_at DATETIME,
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			FOREIGN KEY(created_by) REFERENCES admin_users(id))`,
		`CREATE TABLE IF NOT EXISTS order_accounts (
			id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, account_id INTEGER NOT NULL UNIQUE,
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(order_id,account_id),
			FOREIGN KEY(order_id) REFERENCES orders(id), FOREIGN KEY(account_id) REFERENCES claude_accounts(id))`,
		`CREATE TABLE IF NOT EXISTS order_download_logs (
			id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, admin_user_id INTEGER NOT NULL,
			client_ip TEXT NOT NULL DEFAULT '', created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			FOREIGN KEY(order_id) REFERENCES orders(id), FOREIGN KEY(admin_user_id) REFERENCES admin_users(id))`,
		`CREATE TABLE IF NOT EXISTS audit_logs (
			id INTEGER PRIMARY KEY AUTOINCREMENT, actor_type TEXT NOT NULL, actor_id INTEGER,
			action TEXT NOT NULL, resource_type TEXT NOT NULL, resource_id TEXT NOT NULL DEFAULT '',
			detail_json TEXT NOT NULL DEFAULT '{}', ip TEXT NOT NULL DEFAULT '',
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)`,
		`INSERT OR IGNORE INTO schema_migrations(version) VALUES(1)`,
	}
	for _, statement := range statements {
		if _, err := s.DB.ExecContext(ctx, statement); err != nil {
			return fmt.Errorf("database migration failed: %w", err)
		}
	}
	if err := s.migrateAccountLease(ctx); err != nil {
		return err
	}
	if err := s.migrateCardDispatch(ctx); err != nil {
		return err
	}
	if err := s.migrateAccountCardPool(ctx); err != nil {
		return err
	}
	if err := s.migrateCardCooldown(ctx); err != nil {
		return err
	}
	if err := s.migrateCardReports(ctx); err != nil {
		return err
	}
	if err := s.migrateCardLease(ctx); err != nil {
		return err
	}
	if err := s.migrateAccountHealth(ctx); err != nil {
		return err
	}
	if err := s.migrateGoogleAccounts(ctx); err != nil {
		return err
	}
	if err := s.migrateGoogleAccountReports(ctx); err != nil {
		return err
	}
	if err := s.migrateMailAccounts(ctx); err != nil {
		return err
	}
	if err := s.migrateRegistrationRuns(ctx); err != nil {
		return err
	}
	if err := s.migrateRegistrationSchedule(ctx); err != nil {
		return err
	}
	if err := s.migrateChatGPTCDKs(ctx); err != nil {
		return err
	}
	if err := s.migrateProductOrders(ctx); err != nil {
		return err
	}
	if err := s.migrateAPIKeySoftDelete(ctx); err != nil {
		return err
	}
	return s.migrateChatGPTTasks(ctx)
}

func (s *Store) migrateChatGPTCDKs(ctx context.Context) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS chatgpt_cdks (
			id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL COLLATE NOCASE UNIQUE,
			sku TEXT NOT NULL CHECK(sku IN ('plus','pro','prolite')),
			status TEXT NOT NULL DEFAULT 'available' CHECK(status IN ('available','redeeming','used')),
			order_no TEXT NOT NULL DEFAULT '', remark TEXT NOT NULL DEFAULT '', used_at DATETIME, task_id INTEGER,
			created_by INTEGER NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			FOREIGN KEY(created_by) REFERENCES admin_users(id))`,
		`CREATE INDEX IF NOT EXISTS idx_chatgpt_cdks_filter ON chatgpt_cdks(sku,status,created_at)`,
		`INSERT OR IGNORE INTO schema_migrations(version) VALUES(12)`,
	}
	for _, statement := range statements {
		if _, err := s.DB.ExecContext(ctx, statement); err != nil {
			return fmt.Errorf("chatgpt cdk migration failed: %w", err)
		}
	}
	columns := []struct{ name, definition string }{
		{"order_no", `TEXT NOT NULL DEFAULT ''`},
		{"remark", `TEXT NOT NULL DEFAULT ''`},
		{"task_id", `INTEGER`},
	}
	for _, column := range columns {
		exists, err := s.columnExists(ctx, "chatgpt_cdks", column.name)
		if err != nil {
			return err
		}
		if !exists {
			if _, err = s.DB.ExecContext(ctx, `ALTER TABLE chatgpt_cdks ADD COLUMN `+column.name+` `+column.definition); err != nil {
				return fmt.Errorf("add chatgpt_cdks.%s: %w", column.name, err)
			}
		}
	}
	legacyOrder, err := s.columnExists(ctx, "chatgpt_cdks", "related_order")
	if err != nil {
		return err
	}
	if legacyOrder {
		if _, err = s.DB.ExecContext(ctx, `UPDATE chatgpt_cdks SET order_no=related_order WHERE order_no='' AND related_order<>''`); err != nil {
			return err
		}
	}
	return nil
}

func (s *Store) migrateChatGPTTasks(ctx context.Context) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS chatgpt_tasks (
			id INTEGER PRIMARY KEY AUTOINCREMENT, hash_id TEXT NOT NULL UNIQUE, cdk_id INTEGER NOT NULL, cdk_code TEXT NOT NULL,
			user_email TEXT NOT NULL DEFAULT '', session TEXT NOT NULL, sku TEXT NOT NULL,
			channel TEXT NOT NULL, api_key_id INTEGER, remote_task_id TEXT NOT NULL DEFAULT '',
			status TEXT NOT NULL DEFAULT 'creating', result_json TEXT NOT NULL DEFAULT '',
			error_code TEXT NOT NULL DEFAULT '', error_message TEXT NOT NULL DEFAULT '', client_ip TEXT NOT NULL DEFAULT '',
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			finished_at DATETIME, FOREIGN KEY(cdk_id) REFERENCES chatgpt_cdks(id), FOREIGN KEY(api_key_id) REFERENCES api_keys(id))`,
		`CREATE UNIQUE INDEX IF NOT EXISTS idx_chatgpt_tasks_remote ON chatgpt_tasks(remote_task_id) WHERE remote_task_id<>''`,
		`CREATE INDEX IF NOT EXISTS idx_chatgpt_tasks_cdk ON chatgpt_tasks(cdk_id,created_at)`,
		`CREATE INDEX IF NOT EXISTS idx_chatgpt_tasks_status ON chatgpt_tasks(status,updated_at)`,
		`CREATE INDEX IF NOT EXISTS idx_chatgpt_tasks_email ON chatgpt_tasks(user_email,created_at)`,
		`CREATE INDEX IF NOT EXISTS idx_chatgpt_cdks_local_task ON chatgpt_cdks(task_id)`,
		`INSERT OR IGNORE INTO schema_migrations(version) VALUES(15)`,
	}
	for _, statement := range statements {
		if _, err := s.DB.ExecContext(ctx, statement); err != nil {
			return fmt.Errorf("chatgpt task migration failed: %w", err)
		}
	}
	hashExists, err := s.columnExists(ctx, "chatgpt_tasks", "hash_id")
	if err != nil {
		return err
	}
	if !hashExists {
		if _, err = s.DB.ExecContext(ctx, `ALTER TABLE chatgpt_tasks ADD COLUMN hash_id TEXT NOT NULL DEFAULT ''`); err != nil {
			return fmt.Errorf("add chatgpt_tasks.hash_id: %w", err)
		}
	}
	if _, err = s.DB.ExecContext(ctx, `UPDATE chatgpt_tasks SET hash_id='ctk_'||lower(hex(randomblob(20))) WHERE hash_id=''`); err != nil {
		return err
	}
	if _, err = s.DB.ExecContext(ctx, `CREATE UNIQUE INDEX IF NOT EXISTS idx_chatgpt_tasks_hash ON chatgpt_tasks(hash_id)`); err != nil {
		return err
	}
	legacyTaskID, err := s.columnExists(ctx, "chatgpt_cdks", "redeem_task_id")
	if err != nil {
		return err
	}
	if legacyTaskID {
		legacySQL := `INSERT OR IGNORE INTO chatgpt_tasks(hash_id,cdk_id,cdk_code,user_email,session,sku,channel,api_key_id,remote_task_id,status,error_code,error_message,created_at,updated_at,finished_at)
			SELECT 'ctk_'||lower(hex(randomblob(20))),id,code,'','',sku,'official',redeemed_by_api_key_id,redeem_task_id,
			CASE WHEN task_status='' THEN 'pending' ELSE task_status END,task_error_code,task_error_message,COALESCE(used_at,created_at),updated_at,
			CASE WHEN task_status IN ('success','failed') THEN updated_at ELSE NULL END
			FROM chatgpt_cdks WHERE redeem_task_id<>''`
		if _, err = s.DB.ExecContext(ctx, legacySQL); err != nil {
			return fmt.Errorf("migrate legacy chatgpt tasks: %w", err)
		}
		if _, err = s.DB.ExecContext(ctx, `UPDATE chatgpt_cdks SET task_id=(SELECT t.id FROM chatgpt_tasks t WHERE t.remote_task_id=chatgpt_cdks.redeem_task_id) WHERE task_id IS NULL AND redeem_task_id<>''`); err != nil {
			return err
		}
		if _, err = s.DB.ExecContext(ctx, `UPDATE chatgpt_cdks SET redeem_task_id='',redeemed_by_api_key_id=NULL,task_status='',task_error_code='',task_error_message='' WHERE task_id IS NOT NULL`); err != nil {
			return err
		}
	}
	_, err = s.DB.ExecContext(ctx, `UPDATE chatgpt_cdks SET status='available',task_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE status='redeeming' AND task_id IS NULL AND updated_at<=datetime('now','-2 minutes')`)
	return err
}

func (s *Store) migrateProductOrders(ctx context.Context) error {
	columns := []struct{ name, definition string }{
		{"product_type", `TEXT NOT NULL DEFAULT 'claude_account' CHECK(product_type IN ('claude_account','chatgpt_cdk'))`},
		{"cdk_sku", `TEXT NOT NULL DEFAULT '' CHECK(cdk_sku IN ('','plus','pro','prolite'))`},
	}
	for _, column := range columns {
		exists, err := s.columnExists(ctx, "orders", column.name)
		if err != nil {
			return err
		}
		if !exists {
			if _, err = s.DB.ExecContext(ctx, `ALTER TABLE orders ADD COLUMN `+column.name+` `+column.definition); err != nil {
				return fmt.Errorf("add orders.%s: %w", column.name, err)
			}
		}
	}
	statements := []string{
		`CREATE TABLE IF NOT EXISTS order_cdks (
			id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, cdk_id INTEGER NOT NULL UNIQUE,
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(order_id,cdk_id),
			FOREIGN KEY(order_id) REFERENCES orders(id), FOREIGN KEY(cdk_id) REFERENCES chatgpt_cdks(id))`,
		`CREATE INDEX IF NOT EXISTS idx_order_cdks_order ON order_cdks(order_id)`,
		`CREATE INDEX IF NOT EXISTS idx_orders_product ON orders(product_type,cdk_sku,status)`,
		`INSERT OR IGNORE INTO schema_migrations(version) VALUES(13)`,
	}
	for _, statement := range statements {
		if _, err := s.DB.ExecContext(ctx, statement); err != nil {
			return fmt.Errorf("product order migration failed: %w", err)
		}
	}
	return nil
}

func (s *Store) migrateAPIKeySoftDelete(ctx context.Context) error {
	exists, err := s.columnExists(ctx, "api_keys", "deleted_at")
	if err != nil {
		return err
	}
	if !exists {
		if _, err = s.DB.ExecContext(ctx, `ALTER TABLE api_keys ADD COLUMN deleted_at DATETIME`); err != nil {
			return fmt.Errorf("add api_keys.deleted_at: %w", err)
		}
	}
	if _, err = s.DB.ExecContext(ctx, `CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(deleted_at,status)`); err != nil {
		return err
	}
	_, err = s.DB.ExecContext(ctx, `INSERT OR IGNORE INTO schema_migrations(version) VALUES(14)`)
	return err
}

func (s *Store) migrateAccountLease(ctx context.Context) error {
	columns := []struct{ name, definition string }{
		{"delivery_status", `TEXT NOT NULL DEFAULT 'available'`},
		{"locked_until", `INTEGER`},
		{"lock_request_id", `TEXT NOT NULL DEFAULT ''`},
		{"delivered_at", `DATETIME`},
	}
	for _, column := range columns {
		exists, err := s.columnExists(ctx, "claude_accounts", column.name)
		if err != nil {
			return err
		}
		if exists {
			continue
		}
		if _, err = s.DB.ExecContext(ctx, `ALTER TABLE claude_accounts ADD COLUMN `+column.name+` `+column.definition); err != nil {
			return fmt.Errorf("add claude_accounts.%s: %w", column.name, err)
		}
	}
	if _, err := s.DB.ExecContext(ctx, `CREATE INDEX IF NOT EXISTS idx_claude_account_lease ON claude_accounts(plan,status,delivery_status,locked_until)`); err != nil {
		return err
	}
	if _, err := s.DB.ExecContext(ctx, `UPDATE claude_accounts SET delivery_status='sold',delivered_at=COALESCE(delivered_at,CURRENT_TIMESTAMP) WHERE EXISTS(SELECT 1 FROM order_accounts oa WHERE oa.account_id=claude_accounts.id)`); err != nil {
		return err
	}
	if _, err := s.DB.ExecContext(ctx, `UPDATE claude_accounts SET delivery_status='upgraded',locked_until=NULL,delivered_at=NULL WHERE upgraded_at IS NOT NULL AND NOT EXISTS(SELECT 1 FROM order_accounts oa WHERE oa.account_id=claude_accounts.id)`); err != nil {
		return err
	}
	_, err := s.DB.ExecContext(ctx, `INSERT OR IGNORE INTO schema_migrations(version) VALUES(2)`)
	return err
}

func (s *Store) migrateCardDispatch(ctx context.Context) error {
	exists, err := s.columnExists(ctx, "card_pool", "last_dispatched_at")
	if err != nil {
		return err
	}
	if !exists {
		if _, err = s.DB.ExecContext(ctx, `ALTER TABLE card_pool ADD COLUMN last_dispatched_at DATETIME`); err != nil {
			return fmt.Errorf("add card_pool.last_dispatched_at: %w", err)
		}
	}
	statements := []string{
		`CREATE INDEX IF NOT EXISTS idx_card_pool_dispatch ON card_pool(status,source,last_dispatched_at,usage_count)`,
		`CREATE TABLE IF NOT EXISTS card_dispatches (
			id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, api_key_id INTEGER NOT NULL,
			card_pool_id INTEGER NOT NULL, client_ip TEXT NOT NULL DEFAULT '', created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			UNIQUE(request_id,card_pool_id), FOREIGN KEY(api_key_id) REFERENCES api_keys(id),
			FOREIGN KEY(card_pool_id) REFERENCES card_pool(id))`,
		`CREATE INDEX IF NOT EXISTS idx_card_dispatch_request ON card_dispatches(request_id,api_key_id)`,
		`CREATE INDEX IF NOT EXISTS idx_card_dispatch_owner ON card_dispatches(api_key_id,card_pool_id)`,
		`INSERT OR IGNORE INTO schema_migrations(version) VALUES(3)`,
	}
	for _, statement := range statements {
		if _, err = s.DB.ExecContext(ctx, statement); err != nil {
			return fmt.Errorf("card dispatch migration failed: %w", err)
		}
	}
	return nil
}

func (s *Store) migrateAccountCardPool(ctx context.Context) error {
	exists, err := s.columnExists(ctx, "claude_accounts", "card_pool_id")
	if err != nil {
		return err
	}
	if !exists {
		if _, err = s.DB.ExecContext(ctx, `ALTER TABLE claude_accounts ADD COLUMN card_pool_id INTEGER`); err != nil {
			return fmt.Errorf("add claude_accounts.card_pool_id: %w", err)
		}
	}
	statements := []string{
		`CREATE INDEX IF NOT EXISTS idx_claude_account_card_pool ON claude_accounts(card_pool_id)`,
		`INSERT OR IGNORE INTO schema_migrations(version) VALUES(4)`,
	}
	for _, statement := range statements {
		if _, err = s.DB.ExecContext(ctx, statement); err != nil {
			return fmt.Errorf("account card pool migration failed: %w", err)
		}
	}
	return nil
}

func (s *Store) migrateCardCooldown(ctx context.Context) error {
	exists, err := s.columnExists(ctx, "card_pool", "cooldown_until")
	if err != nil {
		return err
	}
	if !exists {
		if _, err = s.DB.ExecContext(ctx, `ALTER TABLE card_pool ADD COLUMN cooldown_until DATETIME`); err != nil {
			return fmt.Errorf("add card_pool.cooldown_until: %w", err)
		}
	}
	statements := []string{
		`CREATE INDEX IF NOT EXISTS idx_card_pool_cooldown ON card_pool(status,cooldown_until,source,usage_count,last_dispatched_at)`,
		`INSERT OR IGNORE INTO schema_migrations(version) VALUES(5)`,
	}
	for _, statement := range statements {
		if _, err = s.DB.ExecContext(ctx, statement); err != nil {
			return fmt.Errorf("card cooldown migration failed: %w", err)
		}
	}
	return nil
}

func (s *Store) migrateCardReports(ctx context.Context) error {
	columns := []struct {
		name       string
		definition string
	}{
		{"report_status", `TEXT NOT NULL DEFAULT '' CHECK(report_status IN ('','used','unavailable'))`},
		{"report_reason", `TEXT NOT NULL DEFAULT ''`},
		{"reported_at", `DATETIME`},
	}
	for _, column := range columns {
		exists, err := s.columnExists(ctx, "card_dispatches", column.name)
		if err != nil {
			return err
		}
		if exists {
			continue
		}
		if _, err = s.DB.ExecContext(ctx, `ALTER TABLE card_dispatches ADD COLUMN `+column.name+` `+column.definition); err != nil {
			return fmt.Errorf("add card_dispatches.%s: %w", column.name, err)
		}
	}
	_, err := s.DB.ExecContext(ctx, `INSERT OR IGNORE INTO schema_migrations(version) VALUES(16)`)
	return err
}

func (s *Store) migrateCardLease(ctx context.Context) error {
	columns := []struct {
		name       string
		definition string
	}{
		{"locked_until", `INTEGER`},
		{"lock_request_id", `TEXT NOT NULL DEFAULT ''`},
	}
	for _, column := range columns {
		exists, err := s.columnExists(ctx, "card_pool", column.name)
		if err != nil {
			return err
		}
		if exists {
			continue
		}
		if _, err = s.DB.ExecContext(ctx, `ALTER TABLE card_pool ADD COLUMN `+column.name+` `+column.definition); err != nil {
			return fmt.Errorf("add card_pool.%s: %w", column.name, err)
		}
	}
	if _, err := s.DB.ExecContext(ctx, `CREATE INDEX IF NOT EXISTS idx_card_pool_lease ON card_pool(status,source,locked_until,cooldown_until,usage_count,last_dispatched_at)`); err != nil {
		return err
	}
	_, err := s.DB.ExecContext(ctx, `INSERT OR IGNORE INTO schema_migrations(version) VALUES(18)`)
	return err
}

func (s *Store) migrateAccountHealth(ctx context.Context) error {
	columns := []struct{ name, definition string }{
		{"alive_status", `TEXT NOT NULL DEFAULT 'unchecked'`},
		{"alive_checked_at", `DATETIME`},
	}
	for _, column := range columns {
		exists, err := s.columnExists(ctx, "claude_accounts", column.name)
		if err != nil {
			return err
		}
		if exists {
			continue
		}
		if _, err = s.DB.ExecContext(ctx, `ALTER TABLE claude_accounts ADD COLUMN `+column.name+` `+column.definition); err != nil {
			return fmt.Errorf("add claude_accounts.%s: %w", column.name, err)
		}
	}
	if _, err := s.DB.ExecContext(ctx, `CREATE INDEX IF NOT EXISTS idx_claude_account_alive ON claude_accounts(alive_status,alive_checked_at)`); err != nil {
		return err
	}
	_, err := s.DB.ExecContext(ctx, `INSERT OR IGNORE INTO schema_migrations(version) VALUES(6)`)
	return err
}

func (s *Store) migrateGoogleAccounts(ctx context.Context) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS google_accounts (
			id INTEGER PRIMARY KEY AUTOINCREMENT, mail TEXT NOT NULL COLLATE NOCASE UNIQUE,
			password TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unused' CHECK(status IN ('unused','used')),
			enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (1,-1)),
			dispatch_count INTEGER NOT NULL DEFAULT 0, last_dispatched_at DATETIME,
			locked_until INTEGER, lock_request_id TEXT NOT NULL DEFAULT '',
			claude_account_id INTEGER, used_at DATETIME,
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			FOREIGN KEY(claude_account_id) REFERENCES claude_accounts(id))`,
		`CREATE INDEX IF NOT EXISTS idx_google_account_dispatch ON google_accounts(status,locked_until,last_dispatched_at)`,
		`CREATE INDEX IF NOT EXISTS idx_google_account_claude ON google_accounts(claude_account_id)`,
		`CREATE TABLE IF NOT EXISTS google_account_dispatches (
			id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, api_key_id INTEGER NOT NULL,
			google_account_id INTEGER NOT NULL, client_ip TEXT NOT NULL DEFAULT '',
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			UNIQUE(api_key_id,request_id), FOREIGN KEY(api_key_id) REFERENCES api_keys(id),
			FOREIGN KEY(google_account_id) REFERENCES google_accounts(id))`,
		`CREATE INDEX IF NOT EXISTS idx_google_dispatch_account ON google_account_dispatches(google_account_id)`,
		`INSERT OR IGNORE INTO schema_migrations(version) VALUES(7)`,
	}
	for _, statement := range statements {
		if _, err := s.DB.ExecContext(ctx, statement); err != nil {
			return fmt.Errorf("google account migration failed: %w", err)
		}
	}
	exists, err := s.columnExists(ctx, "google_accounts", "enabled")
	if err != nil {
		return err
	}
	if !exists {
		if _, err = s.DB.ExecContext(ctx, `ALTER TABLE google_accounts ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (1,-1))`); err != nil {
			return fmt.Errorf("add google_accounts.enabled: %w", err)
		}
	}
	if _, err = s.DB.ExecContext(ctx, `CREATE INDEX IF NOT EXISTS idx_google_account_enabled_dispatch ON google_accounts(enabled,status,locked_until,last_dispatched_at)`); err != nil {
		return err
	}
	if _, err = s.DB.ExecContext(ctx, `INSERT OR IGNORE INTO schema_migrations(version) VALUES(8)`); err != nil {
		return err
	}
	return nil
}

func (s *Store) migrateGoogleAccountReports(ctx context.Context) error {
	exists, err := s.columnExists(ctx, "google_accounts", "report_status")
	if err != nil {
		return err
	}
	if !exists {
		if _, err = s.DB.ExecContext(ctx, `ALTER TABLE google_accounts ADD COLUMN report_status TEXT NOT NULL DEFAULT '' CHECK(report_status IN ('','used','discarded','login_failed'))`); err != nil {
			return fmt.Errorf("add google_accounts.report_status: %w", err)
		}
	}
	if _, err = s.DB.ExecContext(ctx, `UPDATE google_accounts SET report_status='used' WHERE status='used' AND report_status=''`); err != nil {
		return fmt.Errorf("backfill google account report status: %w", err)
	}
	if _, err = s.DB.ExecContext(ctx, `CREATE INDEX IF NOT EXISTS idx_google_account_report_status ON google_accounts(report_status)`); err != nil {
		return err
	}
	_, err = s.DB.ExecContext(ctx, `INSERT OR IGNORE INTO schema_migrations(version) VALUES(17)`)
	return err
}

func (s *Store) migrateMailAccounts(ctx context.Context) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS mail_accounts (
			id INTEGER PRIMARY KEY AUTOINCREMENT, mail TEXT NOT NULL COLLATE NOCASE UNIQUE,
			password TEXT NOT NULL, platform TEXT NOT NULL COLLATE NOCASE,
			status TEXT NOT NULL DEFAULT 'unused' CHECK(status IN ('unused','used')),
			enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (1,-1)),
			dispatch_count INTEGER NOT NULL DEFAULT 0, last_dispatched_at DATETIME,
			locked_until INTEGER, lock_request_id TEXT NOT NULL DEFAULT '',
			claude_account_id INTEGER, used_at DATETIME,
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			FOREIGN KEY(claude_account_id) REFERENCES claude_accounts(id))`,
		`CREATE INDEX IF NOT EXISTS idx_mail_account_dispatch ON mail_accounts(enabled,status,platform,locked_until,last_dispatched_at)`,
		`CREATE INDEX IF NOT EXISTS idx_mail_account_claude ON mail_accounts(claude_account_id)`,
		`CREATE TABLE IF NOT EXISTS mail_account_dispatches (
			id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, api_key_id INTEGER NOT NULL,
			mail_account_id INTEGER NOT NULL, client_ip TEXT NOT NULL DEFAULT '',
			created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			UNIQUE(api_key_id,request_id), FOREIGN KEY(api_key_id) REFERENCES api_keys(id),
			FOREIGN KEY(mail_account_id) REFERENCES mail_accounts(id))`,
		`CREATE INDEX IF NOT EXISTS idx_mail_dispatch_account ON mail_account_dispatches(mail_account_id)`,
		`INSERT OR IGNORE INTO schema_migrations(version) VALUES(9)`,
	}
	for _, statement := range statements {
		if _, err := s.DB.ExecContext(ctx, statement); err != nil {
			return fmt.Errorf("mail account migration failed: %w", err)
		}
	}
	return nil
}

func (s *Store) migrateRegistrationRuns(ctx context.Context) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS registration_runs (
			id INTEGER PRIMARY KEY AUTOINCREMENT, upstream_run_id TEXT NOT NULL UNIQUE,
			status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
			platform TEXT NOT NULL, requested_count INTEGER NOT NULL,
			imported_count INTEGER NOT NULL DEFAULT 0, consumed_count INTEGER NOT NULL DEFAULT 0,
			lock_id TEXT NOT NULL, summary_json TEXT NOT NULL DEFAULT '{}', tasks_json TEXT NOT NULL DEFAULT '[]',
			error TEXT NOT NULL DEFAULT '', created_by INTEGER,
			started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at DATETIME,
			FOREIGN KEY(created_by) REFERENCES admin_users(id))`,
		`CREATE INDEX IF NOT EXISTS idx_registration_runs_status ON registration_runs(status,started_at)`,
		`CREATE TABLE IF NOT EXISTS registration_run_accounts (
			registration_run_id INTEGER NOT NULL, mail_account_id INTEGER NOT NULL,
			PRIMARY KEY(registration_run_id,mail_account_id),
			FOREIGN KEY(registration_run_id) REFERENCES registration_runs(id) ON DELETE CASCADE,
			FOREIGN KEY(mail_account_id) REFERENCES mail_accounts(id))`,
		`INSERT OR IGNORE INTO schema_migrations(version) VALUES(10)`,
	}
	for _, statement := range statements {
		if _, err := s.DB.ExecContext(ctx, statement); err != nil {
			return fmt.Errorf("registration run migration failed: %w", err)
		}
	}
	return nil
}

func (s *Store) migrateRegistrationSchedule(ctx context.Context) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS registration_schedule (
			id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT -1 CHECK(enabled IN (1,-1)),
			platform TEXT NOT NULL DEFAULT 'mailcom', account_count INTEGER NOT NULL DEFAULT 1,
			concurrency INTEGER NOT NULL DEFAULT 2, retry_max INTEGER NOT NULL DEFAULT 2,
			proxy_mode TEXT NOT NULL DEFAULT 'configured', proxy_template TEXT NOT NULL DEFAULT '',
			mail_fast_path INTEGER NOT NULL DEFAULT 0 CHECK(mail_fast_path IN (0,1)),
			updated_by INTEGER, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
			FOREIGN KEY(updated_by) REFERENCES admin_users(id))`,
		`INSERT OR IGNORE INTO registration_schedule(id) VALUES(1)`,
		`INSERT OR IGNORE INTO schema_migrations(version) VALUES(11)`,
	}
	for _, statement := range statements {
		if _, err := s.DB.ExecContext(ctx, statement); err != nil {
			return fmt.Errorf("registration schedule migration failed: %w", err)
		}
	}
	return nil
}

func (s *Store) columnExists(ctx context.Context, table, column string) (bool, error) {
	rows, err := s.DB.QueryContext(ctx, `PRAGMA table_info(`+table+`)`)
	if err != nil {
		return false, err
	}
	defer rows.Close()
	for rows.Next() {
		var cid int
		var name, dataType string
		var notNull, primaryKey int
		var defaultValue any
		if err = rows.Scan(&cid, &name, &dataType, &notNull, &defaultValue, &primaryKey); err != nil {
			return false, err
		}
		if name == column {
			return true, nil
		}
	}
	return false, rows.Err()
}

type Admin struct {
	ID           int64      `json:"id"`
	Username     string     `json:"username"`
	PasswordHash string     `json:"-"`
	DisplayName  string     `json:"displayName"`
	Role         string     `json:"role"`
	Status       int        `json:"status"`
	LastLoginAt  *time.Time `json:"lastLoginAt"`
	CreatedAt    time.Time  `json:"createdAt"`
}

func scanAdmin(row interface{ Scan(...any) error }) (*Admin, error) {
	a := &Admin{}
	err := row.Scan(&a.ID, &a.Username, &a.PasswordHash, &a.DisplayName, &a.Role, &a.Status, &a.LastLoginAt, &a.CreatedAt)
	return a, err
}

const adminColumns = `id,username,password_hash,display_name,role,status,last_login_at,created_at`

func qualifiedAdminColumns(alias string) string {
	return alias + `.` + strings.ReplaceAll(adminColumns, ",", ","+alias+".")
}

func (s *Store) AdminByUsername(ctx context.Context, username string) (*Admin, error) {
	return scanAdmin(s.DB.QueryRowContext(ctx, `SELECT `+adminColumns+` FROM admin_users WHERE username=?`, strings.TrimSpace(username)))
}
func (s *Store) AdminByID(ctx context.Context, id int64) (*Admin, error) {
	return scanAdmin(s.DB.QueryRowContext(ctx, `SELECT `+adminColumns+` FROM admin_users WHERE id=?`, id))
}
func (s *Store) AdminCount(ctx context.Context) (int, error) {
	var n int
	err := s.DB.QueryRowContext(ctx, `SELECT count(*) FROM admin_users`).Scan(&n)
	return n, err
}
func (s *Store) CreateAdmin(ctx context.Context, username, hash, name, role string) (int64, error) {
	username = strings.TrimSpace(username)
	if username == "" || hash == "" {
		return 0, errors.New("username and password are required")
	}
	if role == "" {
		role = "admin"
	}
	if role != "admin" && role != "super_admin" {
		return 0, errors.New("invalid role")
	}
	r, err := s.DB.ExecContext(ctx, `INSERT INTO admin_users(username,password_hash,display_name,role) VALUES(?,?,?,?)`, username, hash, strings.TrimSpace(name), role)
	if err != nil {
		return 0, err
	}
	return r.LastInsertId()
}
func (s *Store) CreateSession(ctx context.Context, adminID int64, hash, ip, ua string, expires time.Time) error {
	_, err := s.DB.ExecContext(ctx, `INSERT INTO admin_sessions(admin_user_id,token_hash,ip,user_agent,expires_at) VALUES(?,?,?,?,?)`, adminID, hash, ip, ua, expires.UTC().Unix())
	return err
}
func (s *Store) AdminBySession(ctx context.Context, hash string) (*Admin, error) {
	return scanAdmin(s.DB.QueryRowContext(ctx, `SELECT `+qualifiedAdminColumns("a")+` FROM admin_sessions s JOIN admin_users a ON a.id=s.admin_user_id WHERE s.token_hash=? AND CAST(s.expires_at AS INTEGER)>unixepoch() AND a.status=1`, hash))
}
func (s *Store) DeleteSession(ctx context.Context, hash string) error {
	_, err := s.DB.ExecContext(ctx, `DELETE FROM admin_sessions WHERE token_hash=?`, hash)
	return err
}
func (s *Store) TouchLogin(ctx context.Context, id int64) {
	_, _ = s.DB.ExecContext(ctx, `UPDATE admin_users SET last_login_at=CURRENT_TIMESTAMP WHERE id=?`, id)
}

func NormalizePlan(plan string) (string, error) {
	plan = strings.ToLower(strings.TrimSpace(plan))
	if plan == "" {
		plan = "free"
	}
	if plan != "free" && plan != "max_20x" {
		return "", errors.New("plan must be free or max_20x")
	}
	return plan, nil
}
