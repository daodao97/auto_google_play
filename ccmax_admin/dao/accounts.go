package dao

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"
)

type ClaudeAccount struct {
	ID               int64      `json:"id"`
	Mail             string     `json:"mail"`
	Password         string     `json:"password,omitempty"`
	SessionKey       string     `json:"sessionKey,omitempty"`
	Plan             string     `json:"plan"`
	Status           int        `json:"status"`
	DispatchCount    int        `json:"dispatchCount"`
	LastDispatchedAt *time.Time `json:"lastDispatchedAt"`
	CreatedAt        time.Time  `json:"createdAt"`
	UpgradedAt       *time.Time `json:"upgradedAt"`
	CardPoolID       *int64     `json:"cardPoolId"`
	DeliveryStatus   string     `json:"deliveryStatus"`
	LockedUntil      *time.Time `json:"lockedUntil"`
	LockRequestID    string     `json:"lockRequestId,omitempty"`
	DeliveredAt      *time.Time `json:"deliveredAt"`
	OrderBatchNo     string     `json:"orderBatchNo,omitempty"`
	AliveStatus      string     `json:"aliveStatus"`
	AliveCheckedAt   *time.Time `json:"aliveCheckedAt"`
}

func scanAccountWithOrder(row interface{ Scan(...any) error }) (*ClaudeAccount, error) {
	a := &ClaudeAccount{}
	var lockedUntil sql.NullInt64
	err := row.Scan(&a.ID, &a.Mail, &a.Password, &a.SessionKey, &a.Plan, &a.Status, &a.DispatchCount, &a.LastDispatchedAt, &a.CreatedAt, &a.UpgradedAt, &a.CardPoolID, &a.DeliveryStatus, &lockedUntil, &a.LockRequestID, &a.DeliveredAt, &a.AliveStatus, &a.AliveCheckedAt, &a.OrderBatchNo)
	if err == nil && lockedUntil.Valid {
		value := time.Unix(lockedUntil.Int64, 0)
		a.LockedUntil = &value
	}
	return a, err
}

func scanAccount(row interface{ Scan(...any) error }) (*ClaudeAccount, error) {
	a := &ClaudeAccount{}
	var lockedUntil sql.NullInt64
	err := row.Scan(&a.ID, &a.Mail, &a.Password, &a.SessionKey, &a.Plan, &a.Status, &a.DispatchCount, &a.LastDispatchedAt, &a.CreatedAt, &a.UpgradedAt, &a.CardPoolID, &a.DeliveryStatus, &lockedUntil, &a.LockRequestID, &a.DeliveredAt, &a.AliveStatus, &a.AliveCheckedAt)
	if err == nil && lockedUntil.Valid {
		value := time.Unix(lockedUntil.Int64, 0)
		a.LockedUntil = &value
	}
	return a, err
}

const accountColumns = `id,mail,password,session_key,plan,status,dispatch_count,last_dispatched_at,created_at,upgraded_at,card_pool_id,delivery_status,locked_until,lock_request_id,delivered_at,alive_status,alive_checked_at`

func qualifiedAccountColumns(alias string) string {
	return alias + "." + strings.ReplaceAll(accountColumns, ",", ","+alias+".")
}

func (s *Store) CreateAccount(ctx context.Context, a ClaudeAccount) (int64, error) {
	plan, err := NormalizePlan(a.Plan)
	if err != nil {
		return 0, err
	}
	mail := strings.ToLower(strings.TrimSpace(a.Mail))
	if mail == "" || strings.TrimSpace(a.Password) == "" || strings.TrimSpace(a.SessionKey) == "" {
		return 0, errors.New("mail, password and sessionKey are required")
	}
	if strings.ContainsAny(a.Password, "\r\n") || strings.ContainsAny(a.SessionKey, "\r\n") {
		return 0, errors.New("password and sessionKey cannot contain newlines")
	}
	status := a.Status
	if status == 0 {
		status = 1
	}
	r, err := s.DB.ExecContext(ctx, `INSERT INTO claude_accounts(mail,password,session_key,plan,status,upgraded_at) VALUES(?,?,?,?,?,?)`, mail, a.Password, strings.TrimSpace(a.SessionKey), plan, status, a.UpgradedAt)
	if err != nil {
		return 0, err
	}
	return r.LastInsertId()
}

func (s *Store) UpdateAccount(ctx context.Context, id int64, a ClaudeAccount) error {
	plan, err := NormalizePlan(a.Plan)
	if err != nil {
		return err
	}
	mail := strings.ToLower(strings.TrimSpace(a.Mail))
	if mail == "" || a.Password == "" || strings.TrimSpace(a.SessionKey) == "" {
		return errors.New("mail, password and sessionKey are required")
	}
	if strings.ContainsAny(a.Password, "\r\n") || strings.ContainsAny(a.SessionKey, "\r\n") {
		return errors.New("password and sessionKey cannot contain newlines")
	}
	sessionKey := strings.TrimSpace(a.SessionKey)
	r, err := s.DB.ExecContext(ctx, `UPDATE claude_accounts SET mail=?,password=?,alive_status=CASE WHEN session_key<>? THEN 'unchecked' ELSE alive_status END,alive_checked_at=CASE WHEN session_key<>? THEN NULL ELSE alive_checked_at END,session_key=?,plan=?,status=?,upgraded_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?`, mail, a.Password, sessionKey, sessionKey, sessionKey, plan, a.Status, a.UpgradedAt, id)
	if err != nil {
		return err
	}
	n, _ := r.RowsAffected()
	if n == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *Store) SetAccountStatus(ctx context.Context, id int64, status int) error {
	if status != 1 && status != -1 {
		return errors.New("status must be 1 or -1")
	}
	r, err := s.DB.ExecContext(ctx, `UPDATE claude_accounts SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?`, status, id)
	if err != nil {
		return err
	}
	n, _ := r.RowsAffected()
	if n == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *Store) DeleteAccount(ctx context.Context, id int64) error {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	var orderCount int
	if err = tx.QueryRowContext(ctx, `SELECT count(*) FROM order_accounts WHERE account_id=?`, id).Scan(&orderCount); err != nil {
		return err
	}
	if orderCount > 0 {
		return errors.New("已售出账号关联订单，不能删除")
	}
	if _, err = tx.ExecContext(ctx, `DELETE FROM claude_account_dispatches WHERE account_id=?`, id); err != nil {
		return err
	}
	result, err := tx.ExecContext(ctx, `DELETE FROM claude_accounts WHERE id=?`, id)
	if err != nil {
		return err
	}
	if affected, _ := result.RowsAffected(); affected == 0 {
		return ErrNotFound
	}
	return tx.Commit()
}

func (s *Store) ResetAccount(ctx context.Context, id int64) error {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	var orderCount int
	if err = tx.QueryRowContext(ctx, `SELECT count(*) FROM order_accounts WHERE account_id=?`, id).Scan(&orderCount); err != nil {
		return err
	}
	if orderCount > 0 {
		return errors.New("已售出账号关联订单，不能重置")
	}
	result, err := tx.ExecContext(ctx, `UPDATE claude_accounts SET plan='free',upgraded_at=NULL,card_pool_id=NULL,delivery_status='available',locked_until=NULL,lock_request_id='',delivered_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?`, id)
	if err != nil {
		return err
	}
	if affected, _ := result.RowsAffected(); affected == 0 {
		return ErrNotFound
	}
	return tx.Commit()
}

func (s *Store) ListAccounts(ctx context.Context, page, size int, query, plan string, status int) ([]ClaudeAccount, int, error) {
	if _, err := s.ReleaseExpiredLeases(ctx); err != nil {
		return nil, 0, err
	}
	where := ` WHERE 1=1`
	args := []any{}
	if strings.TrimSpace(query) != "" {
		where += ` AND mail LIKE ?`
		args = append(args, "%"+strings.TrimSpace(query)+"%")
	}
	if strings.TrimSpace(plan) != "" {
		where += ` AND plan=?`
		args = append(args, strings.TrimSpace(plan))
	}
	if status != 0 {
		where += ` AND status=?`
		args = append(args, status)
	}
	var total int
	if err := s.DB.QueryRowContext(ctx, `SELECT count(*) FROM claude_accounts`+where, args...).Scan(&total); err != nil {
		return nil, 0, err
	}
	args = append(args, size, (page-1)*size)
	rows, err := s.DB.QueryContext(ctx, `SELECT `+accountColumns+`,COALESCE((SELECT o.batch_no FROM order_accounts oa JOIN orders o ON o.id=oa.order_id WHERE oa.account_id=claude_accounts.id LIMIT 1),'') FROM claude_accounts`+where+` ORDER BY id DESC LIMIT ? OFFSET ?`, args...)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	items := []ClaudeAccount{}
	for rows.Next() {
		a, err := scanAccountWithOrder(rows)
		if err != nil {
			return nil, 0, err
		}
		items = append(items, *a)
	}
	return items, total, rows.Err()
}

func (s *Store) UpgradeAccount(ctx context.Context, mail, plan string, upgradedAt time.Time, cardPoolID int64) (*ClaudeAccount, error) {
	plan, err := NormalizePlan(plan)
	if err != nil {
		return nil, err
	}
	if plan != "max_20x" {
		return nil, errors.New("public sync can only upgrade to max_20x")
	}
	if upgradedAt.IsZero() {
		upgradedAt = time.Now()
	}
	if cardPoolID <= 0 {
		return nil, errors.New("positive cardPoolId is required")
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	var alreadyReported int
	if err = tx.QueryRowContext(ctx, `SELECT count(*) FROM claude_accounts WHERE mail=? COLLATE NOCASE AND plan='max_20x' AND delivery_status='upgraded' AND card_pool_id=?`, strings.TrimSpace(mail), cardPoolID).Scan(&alreadyReported); err != nil {
		return nil, err
	}
	r, err := tx.ExecContext(ctx, `UPDATE claude_accounts SET plan=?,upgraded_at=?,card_pool_id=?,delivery_status='upgraded',locked_until=NULL,delivered_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE mail=? COLLATE NOCASE`, plan, upgradedAt, cardPoolID, strings.TrimSpace(mail))
	if err != nil {
		return nil, err
	}
	if affected, _ := r.RowsAffected(); affected == 0 {
		return nil, ErrNotFound
	}
	usageIncrement := 1
	if alreadyReported > 0 {
		usageIncrement = 0
	}
	if _, err = tx.ExecContext(ctx, `UPDATE card_pool SET usage_count=usage_count+?,cooldown_until=?,updated_at=CURRENT_TIMESTAMP WHERE id=?`, usageIncrement, time.Now().Add(5*time.Hour), cardPoolID); err != nil {
		return nil, err
	}
	a, err := scanAccount(tx.QueryRowContext(ctx, `SELECT `+accountColumns+` FROM claude_accounts WHERE mail=? COLLATE NOCASE`, strings.TrimSpace(mail)))
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return a, nil
}

type APIKey struct {
	ID         int64      `json:"id"`
	Name       string     `json:"name"`
	Prefix     string     `json:"prefix"`
	Status     int        `json:"status"`
	LastUsedAt *time.Time `json:"lastUsedAt"`
	ExpiresAt  *time.Time `json:"expiresAt"`
	CreatedAt  time.Time  `json:"createdAt"`
}

func (s *Store) CreateAPIKey(ctx context.Context, name, prefix, hash string, adminID int64) (int64, error) {
	r, err := s.DB.ExecContext(ctx, `INSERT INTO api_keys(name,key_prefix,key_hash,created_by) VALUES(?,?,?,?)`, strings.TrimSpace(name), prefix, hash, adminID)
	if err != nil {
		return 0, err
	}
	return r.LastInsertId()
}
func (s *Store) APIKeyByHash(ctx context.Context, hash string) (*APIKey, error) {
	k := &APIKey{}
	err := s.DB.QueryRowContext(ctx, `SELECT id,name,key_prefix,status,last_used_at,expires_at,created_at FROM api_keys WHERE key_hash=? AND deleted_at IS NULL AND status=1 AND (expires_at IS NULL OR datetime(expires_at)>CURRENT_TIMESTAMP)`, hash).Scan(&k.ID, &k.Name, &k.Prefix, &k.Status, &k.LastUsedAt, &k.ExpiresAt, &k.CreatedAt)
	return k, err
}
func (s *Store) TouchAPIKey(ctx context.Context, id int64) {
	_, _ = s.DB.ExecContext(ctx, `UPDATE api_keys SET last_used_at=CURRENT_TIMESTAMP WHERE id=?`, id)
}

func (s *Store) DispatchAccounts(ctx context.Context, apiKeyID int64, requestID string, count int, lease time.Duration, ip string) ([]ClaudeAccount, error) {
	if lease <= 0 {
		lease = 30 * time.Minute
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `UPDATE claude_accounts SET delivery_status='available',locked_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE delivery_status='locked' AND locked_until IS NOT NULL AND locked_until<=unixepoch()`); err != nil {
		return nil, err
	}
	// An idempotent retry returns the exact original account set.
	rows, err := tx.QueryContext(ctx, `SELECT `+qualifiedAccountColumns("a")+` FROM claude_account_dispatches d JOIN claude_accounts a ON a.id=d.account_id WHERE d.request_id=? AND d.api_key_id=? ORDER BY d.id`, requestID, apiKeyID)
	if err != nil {
		return nil, err
	}
	existing := []ClaudeAccount{}
	for rows.Next() {
		a, e := scanAccount(rows)
		if e != nil {
			rows.Close()
			return nil, e
		}
		existing = append(existing, *a)
	}
	rows.Close()
	if len(existing) > 0 {
		for _, item := range existing {
			if item.LockRequestID != requestID || item.DeliveryStatus == "available" || item.AliveStatus == "dead" {
				return nil, errors.New("idempotency lease has expired or been released")
			}
		}
		if err := tx.Commit(); err != nil {
			return nil, err
		}
		return existing, nil
	}
	rows, err = tx.QueryContext(ctx, `SELECT `+accountColumns+` FROM claude_accounts a WHERE a.plan='free' AND a.status=1 AND a.alive_status<>'dead' AND a.delivery_status='available' AND NOT EXISTS(SELECT 1 FROM order_accounts oa WHERE oa.account_id=a.id) ORDER BY a.last_dispatched_at ASC,a.id ASC LIMIT ?`, count)
	if err != nil {
		return nil, err
	}
	items := []ClaudeAccount{}
	for rows.Next() {
		a, e := scanAccount(rows)
		if e != nil {
			rows.Close()
			return nil, e
		}
		items = append(items, *a)
	}
	rows.Close()
	if len(items) != count {
		return nil, fmt.Errorf("insufficient accounts: available=%d requested=%d", len(items), count)
	}
	now := time.Now()
	lockedUntil := now.Add(lease).Truncate(time.Second)
	for i := range items {
		var result sql.Result
		result, err = tx.ExecContext(ctx, `UPDATE claude_accounts SET delivery_status='locked',locked_until=?,lock_request_id=?,last_dispatched_at=?,dispatch_count=dispatch_count+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status=1 AND alive_status<>'dead' AND delivery_status='available'`, lockedUntil.Unix(), requestID, now, items[i].ID)
		if err != nil {
			return nil, err
		}
		if affected, _ := result.RowsAffected(); affected != 1 {
			return nil, errors.New("account allocation conflict; please retry")
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO claude_account_dispatches(request_id,api_key_id,account_id,requested_plan,client_ip) VALUES(?,?,?,?,?)`, requestID, apiKeyID, items[i].ID, "free", ip)
		if err != nil {
			return nil, err
		}
		items[i].LastDispatchedAt = &now
		items[i].DispatchCount++
		items[i].DeliveryStatus = "locked"
		items[i].LockedUntil = &lockedUntil
		items[i].LockRequestID = requestID
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return items, nil
}

func (s *Store) ReleaseExpiredLeases(ctx context.Context) (int64, error) {
	r, err := s.DB.ExecContext(ctx, `UPDATE claude_accounts SET delivery_status='available',locked_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE delivery_status='locked' AND locked_until IS NOT NULL AND locked_until<=unixepoch()`)
	if err != nil {
		return 0, err
	}
	return r.RowsAffected()
}

func (s *Store) ReleaseAccountLease(ctx context.Context, id int64) error {
	r, err := s.DB.ExecContext(ctx, `UPDATE claude_accounts SET delivery_status='available',locked_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND delivery_status='locked'`, id)
	if err != nil {
		return err
	}
	if affected, _ := r.RowsAffected(); affected == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *Store) ReleaseDispatchedAccount(ctx context.Context, apiKeyID int64, requestID, mail string) (*ClaudeAccount, error) {
	requestID = strings.TrimSpace(requestID)
	mail = strings.ToLower(strings.TrimSpace(mail))
	if requestID == "" || mail == "" {
		return nil, errors.New("requestId and mail are required")
	}
	if _, err := s.ReleaseExpiredLeases(ctx); err != nil {
		return nil, err
	}
	var accountID int64
	err := s.DB.QueryRowContext(ctx, `SELECT a.id FROM claude_account_dispatches d JOIN claude_accounts a ON a.id=d.account_id WHERE d.api_key_id=? AND d.request_id=? AND a.mail=? COLLATE NOCASE`, apiKeyID, requestID, mail).Scan(&accountID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	r, err := s.DB.ExecContext(ctx, `UPDATE claude_accounts SET delivery_status='available',locked_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND delivery_status='locked' AND lock_request_id=?`, accountID, requestID)
	if err != nil {
		return nil, err
	}
	if affected, _ := r.RowsAffected(); affected == 0 {
		var status, lockID string
		if err := s.DB.QueryRowContext(ctx, `SELECT delivery_status,lock_request_id FROM claude_accounts WHERE id=?`, accountID).Scan(&status, &lockID); err != nil {
			return nil, err
		}
		if lockID != requestID || status != "available" {
			return nil, errors.New("lease is expired or has been reassigned")
		}
	}
	return scanAccount(s.DB.QueryRowContext(ctx, `SELECT `+accountColumns+` FROM claude_accounts WHERE id=?`, accountID))
}

func (s *Store) AccountsByIDs(ctx context.Context, ids []int64) ([]ClaudeAccount, error) {
	if len(ids) == 0 {
		return []ClaudeAccount{}, nil
	}
	placeholders := make([]string, len(ids))
	args := make([]any, len(ids))
	for i, id := range ids {
		placeholders[i] = "?"
		args[i] = id
	}
	rows, err := s.DB.QueryContext(ctx, `SELECT `+accountColumns+` FROM claude_accounts WHERE id IN (`+strings.Join(placeholders, ",")+`) ORDER BY id`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []ClaudeAccount{}
	for rows.Next() {
		account, scanErr := scanAccount(rows)
		if scanErr != nil {
			return nil, scanErr
		}
		items = append(items, *account)
	}
	return items, rows.Err()
}

func (s *Store) SetAccountAliveStatus(ctx context.Context, id int64, status string, checkedAt time.Time) error {
	if status != "alive" && status != "dead" {
		return errors.New("alive status must be alive or dead")
	}
	result, err := s.DB.ExecContext(ctx, `UPDATE claude_accounts SET alive_status=?,alive_checked_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?`, status, checkedAt, id)
	if err != nil {
		return err
	}
	if affected, _ := result.RowsAffected(); affected == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *Store) Dashboard(ctx context.Context) (map[string]any, error) {
	if _, err := s.ReleaseExpiredLeases(ctx); err != nil {
		return nil, err
	}
	result := map[string]any{}
	queries := map[string]string{
		"freeAccounts":   `SELECT count(*) FROM claude_accounts a WHERE plan='free' AND status=1 AND alive_status<>'dead' AND delivery_status='available' AND NOT EXISTS(SELECT 1 FROM order_accounts oa WHERE oa.account_id=a.id)`,
		"maxAccounts":    `SELECT count(*) FROM claude_accounts a WHERE plan='max_20x' AND status=1 AND alive_status<>'dead' AND delivery_status IN ('available','upgraded') AND NOT EXISTS(SELECT 1 FROM order_accounts oa WHERE oa.account_id=a.id)`,
		"plusCDKs":       `SELECT count(*) FROM chatgpt_cdks c WHERE sku='plus' AND status='available' AND NOT EXISTS(SELECT 1 FROM order_cdks oc WHERE oc.cdk_id=c.id)`,
		"proCDKs":        `SELECT count(*) FROM chatgpt_cdks c WHERE sku='pro' AND status='available' AND NOT EXISTS(SELECT 1 FROM order_cdks oc WHERE oc.cdk_id=c.id)`,
		"proliteCDKs":    `SELECT count(*) FROM chatgpt_cdks c WHERE sku='prolite' AND status='available' AND NOT EXISTS(SELECT 1 FROM order_cdks oc WHERE oc.cdk_id=c.id)`,
		"availableCards": `SELECT count(*) FROM card_pool WHERE status=1`, "orders": `SELECT count(*) FROM orders`,
		"todayDispatches": `SELECT count(*) FROM claude_account_dispatches WHERE created_at>=date('now','localtime')`,
		"todaySalesCents": `SELECT COALESCE(sum(sale_price_cents),0) FROM orders WHERE status='allocated' AND created_at>=date('now','localtime')`,
	}
	for key, q := range queries {
		var n int64
		if err := s.DB.QueryRowContext(ctx, q).Scan(&n); err != nil {
			return nil, err
		}
		result[key] = n
	}
	return result, nil
}

func IsUniqueError(err error) bool {
	return err != nil && strings.Contains(strings.ToLower(err.Error()), "unique constraint failed")
}
func IsNoRows(err error) bool { return errors.Is(err, sql.ErrNoRows) }
