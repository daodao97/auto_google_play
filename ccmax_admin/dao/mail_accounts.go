package dao

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"
)

type MailAccount struct {
	ID                int64      `json:"id"`
	Mail              string     `json:"mail"`
	Password          string     `json:"password,omitempty"`
	Platform          string     `json:"platform"`
	Status            string     `json:"status"`
	Enabled           int        `json:"enabled"`
	DispatchCount     int        `json:"dispatchCount"`
	LastDispatchedAt  *time.Time `json:"lastDispatchedAt"`
	LockedUntil       *time.Time `json:"lockedUntil"`
	LockRequestID     string     `json:"lockRequestId,omitempty"`
	ClaudeAccountID   *int64     `json:"claudeAccountId"`
	ClaudeAccountMail string     `json:"claudeAccountMail,omitempty"`
	UsedAt            *time.Time `json:"usedAt"`
	CreatedAt         time.Time  `json:"createdAt"`
}

const mailAccountColumns = `m.id,m.mail,m.password,m.platform,m.status,m.enabled,m.dispatch_count,m.last_dispatched_at,m.locked_until,m.lock_request_id,m.claude_account_id,COALESCE(c.mail,''),m.used_at,m.created_at`

func scanMailAccount(row interface{ Scan(...any) error }) (*MailAccount, error) {
	item := &MailAccount{}
	var lockedUntil sql.NullInt64
	err := row.Scan(&item.ID, &item.Mail, &item.Password, &item.Platform, &item.Status, &item.Enabled, &item.DispatchCount, &item.LastDispatchedAt, &lockedUntil, &item.LockRequestID, &item.ClaudeAccountID, &item.ClaudeAccountMail, &item.UsedAt, &item.CreatedAt)
	if err == nil && lockedUntil.Valid {
		value := time.Unix(lockedUntil.Int64, 0)
		item.LockedUntil = &value
	}
	return item, err
}

func (s *Store) CreateMailAccount(ctx context.Context, mail, password, platform string) (int64, error) {
	mail = strings.ToLower(strings.TrimSpace(mail))
	platform = strings.ToLower(strings.TrimSpace(platform))
	if mail == "" || !strings.Contains(mail, "@") || password == "" || platform == "" {
		return 0, errors.New("valid mail, password and platform are required")
	}
	if strings.ContainsAny(mail, "\r\n|") || strings.ContainsAny(password, "\r\n|") || strings.ContainsAny(platform, "\r\n|") {
		return 0, errors.New("mail, password and platform must be single-line values without pipes")
	}
	result, err := s.DB.ExecContext(ctx, `INSERT INTO mail_accounts(mail,password,platform) VALUES(?,?,?)`, mail, password, platform)
	if err != nil {
		return 0, err
	}
	return result.LastInsertId()
}

func (s *Store) ListMailAccounts(ctx context.Context, page, size int, query, platform, status string, enabled int) ([]MailAccount, int, error) {
	if _, err := s.ReleaseExpiredMailAccountLeases(ctx); err != nil {
		return nil, 0, err
	}
	where := ` WHERE 1=1`
	args := []any{}
	if query = strings.TrimSpace(query); query != "" {
		where += ` AND m.mail LIKE ?`
		args = append(args, "%"+query+"%")
	}
	if platform = strings.ToLower(strings.TrimSpace(platform)); platform != "" {
		where += ` AND m.platform=?`
		args = append(args, platform)
	}
	if status = strings.ToLower(strings.TrimSpace(status)); status != "" {
		if status != "unused" && status != "used" {
			return nil, 0, errors.New("status must be unused or used")
		}
		where += ` AND m.status=?`
		args = append(args, status)
	}
	if enabled != 0 {
		if enabled != 1 && enabled != -1 {
			return nil, 0, errors.New("enabled must be 1 or -1")
		}
		where += ` AND m.enabled=?`
		args = append(args, enabled)
	}
	var total int
	if err := s.DB.QueryRowContext(ctx, `SELECT count(*) FROM mail_accounts m`+where, args...).Scan(&total); err != nil {
		return nil, 0, err
	}
	args = append(args, size, (page-1)*size)
	rows, err := s.DB.QueryContext(ctx, `SELECT `+mailAccountColumns+` FROM mail_accounts m LEFT JOIN claude_accounts c ON c.id=m.claude_account_id`+where+` ORDER BY m.id DESC LIMIT ? OFFSET ?`, args...)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	items := []MailAccount{}
	for rows.Next() {
		item, scanErr := scanMailAccount(rows)
		if scanErr != nil {
			return nil, 0, scanErr
		}
		items = append(items, *item)
	}
	return items, total, rows.Err()
}

func (s *Store) DispatchMailAccount(ctx context.Context, apiKeyID int64, requestID, platform string, lease time.Duration, ip string) (*MailAccount, error) {
	requestID = strings.TrimSpace(requestID)
	platform = strings.ToLower(strings.TrimSpace(platform))
	if requestID == "" {
		return nil, errors.New("requestId is required")
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, `UPDATE mail_accounts SET locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE status='unused' AND locked_until IS NOT NULL AND locked_until<=unixepoch()`); err != nil {
		return nil, err
	}
	item, err := scanMailAccount(tx.QueryRowContext(ctx, `SELECT `+mailAccountColumns+` FROM mail_account_dispatches d JOIN mail_accounts m ON m.id=d.mail_account_id LEFT JOIN claude_accounts c ON c.id=m.claude_account_id WHERE d.api_key_id=? AND d.request_id=?`, apiKeyID, requestID))
	if err == nil {
		if platform != "" && item.Platform != platform {
			return nil, errors.New("idempotency request platform does not match")
		}
		if item.Status == "used" || (item.LockRequestID == requestID && item.LockedUntil != nil && item.LockedUntil.After(time.Now())) {
			if err = tx.Commit(); err != nil {
				return nil, err
			}
			return item, nil
		}
		return nil, errors.New("idempotency lease has expired")
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	query := `SELECT ` + mailAccountColumns + ` FROM mail_accounts m LEFT JOIN claude_accounts c ON c.id=m.claude_account_id WHERE m.enabled=1 AND m.status='unused' AND m.locked_until IS NULL`
	args := []any{}
	if platform != "" {
		query += ` AND m.platform=?`
		args = append(args, platform)
	}
	query += ` ORDER BY m.last_dispatched_at ASC,m.id ASC LIMIT 1`
	item, err = scanMailAccount(tx.QueryRowContext(ctx, query, args...))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, errors.New("insufficient mail accounts")
	}
	if err != nil {
		return nil, err
	}
	now := time.Now()
	lockedUntil := now.Add(lease).Truncate(time.Second)
	result, err := tx.ExecContext(ctx, `UPDATE mail_accounts SET locked_until=?,lock_request_id=?,dispatch_count=dispatch_count+1,last_dispatched_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND enabled=1 AND status='unused' AND locked_until IS NULL`, lockedUntil.Unix(), requestID, now, item.ID)
	if err != nil {
		return nil, err
	}
	if affected, _ := result.RowsAffected(); affected != 1 {
		return nil, errors.New("mail account allocation conflict; please retry")
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO mail_account_dispatches(request_id,api_key_id,mail_account_id,client_ip) VALUES(?,?,?,?)`, requestID, apiKeyID, item.ID, ip); err != nil {
		return nil, err
	}
	item.DispatchCount++
	item.LastDispatchedAt = &now
	item.LockedUntil = &lockedUntil
	item.LockRequestID = requestID
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return item, nil
}

func (s *Store) UseMailAccount(ctx context.Context, apiKeyID int64, requestID string, mailAccountID int64, claudeMail string) (*MailAccount, error) {
	requestID = strings.TrimSpace(requestID)
	claudeMail = strings.ToLower(strings.TrimSpace(claudeMail))
	if requestID == "" || mailAccountID <= 0 || claudeMail == "" {
		return nil, errors.New("requestId, positive mailAccountId and claudeAccountMail are required")
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	var dispatchedID int64
	if err = tx.QueryRowContext(ctx, `SELECT mail_account_id FROM mail_account_dispatches WHERE api_key_id=? AND request_id=?`, apiKeyID, requestID).Scan(&dispatchedID); err != nil {
		return nil, err
	}
	if dispatchedID != mailAccountID {
		return nil, errors.New("mail account does not belong to this dispatch request")
	}
	var claudeID int64
	if err = tx.QueryRowContext(ctx, `SELECT id FROM claude_accounts WHERE mail=? COLLATE NOCASE`, claudeMail).Scan(&claudeID); err != nil {
		return nil, err
	}
	var status, lockRequestID string
	var linkedClaudeID sql.NullInt64
	if err = tx.QueryRowContext(ctx, `SELECT status,lock_request_id,claude_account_id FROM mail_accounts WHERE id=?`, mailAccountID).Scan(&status, &lockRequestID, &linkedClaudeID); err != nil {
		return nil, err
	}
	if status == "used" {
		if linkedClaudeID.Valid && linkedClaudeID.Int64 == claudeID {
			item, scanErr := scanMailAccount(tx.QueryRowContext(ctx, `SELECT `+mailAccountColumns+` FROM mail_accounts m LEFT JOIN claude_accounts c ON c.id=m.claude_account_id WHERE m.id=?`, mailAccountID))
			if scanErr != nil {
				return nil, scanErr
			}
			if err = tx.Commit(); err != nil {
				return nil, err
			}
			return item, nil
		}
		return nil, errors.New("mail account is already used")
	}
	if lockRequestID != requestID {
		return nil, errors.New("mail account lease has expired or been reassigned")
	}
	if _, err = tx.ExecContext(ctx, `UPDATE mail_accounts SET status='used',claude_account_id=?,used_at=CURRENT_TIMESTAMP,locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='unused'`, claudeID, mailAccountID); err != nil {
		return nil, err
	}
	item, err := scanMailAccount(tx.QueryRowContext(ctx, `SELECT `+mailAccountColumns+` FROM mail_accounts m LEFT JOIN claude_accounts c ON c.id=m.claude_account_id WHERE m.id=?`, mailAccountID))
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return item, nil
}

func (s *Store) ReleaseExpiredMailAccountLeases(ctx context.Context) (int64, error) {
	result, err := s.DB.ExecContext(ctx, `UPDATE mail_accounts SET locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE status='unused' AND locked_until IS NOT NULL AND locked_until<=unixepoch()`)
	if err != nil {
		return 0, err
	}
	return result.RowsAffected()
}

func (s *Store) MailAccountStats(ctx context.Context) (map[string]int, error) {
	if _, err := s.ReleaseExpiredMailAccountLeases(ctx); err != nil {
		return nil, err
	}
	result := map[string]int{"unused": 0, "locked": 0, "used": 0, "total": 0}
	rows, err := s.DB.QueryContext(ctx, `SELECT status,enabled,CASE WHEN status='unused' AND locked_until IS NOT NULL THEN 1 ELSE 0 END,count(*) FROM mail_accounts GROUP BY status,enabled,CASE WHEN status='unused' AND locked_until IS NOT NULL THEN 1 ELSE 0 END`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var status string
		var enabled, locked, count int
		if err = rows.Scan(&status, &enabled, &locked, &count); err != nil {
			return nil, err
		}
		result["total"] += count
		if status == "used" {
			result["used"] += count
		} else if enabled != 1 {
			continue
		} else if locked == 1 {
			result["locked"] += count
		} else {
			result["unused"] += count
		}
	}
	return result, rows.Err()
}

func (s *Store) SetMailAccountEnabled(ctx context.Context, id int64, enabled int) error {
	if enabled != 1 && enabled != -1 {
		return errors.New("enabled must be 1 or -1")
	}
	result, err := s.DB.ExecContext(ctx, `UPDATE mail_accounts SET enabled=?,locked_until=CASE WHEN ?=-1 THEN NULL ELSE locked_until END,lock_request_id=CASE WHEN ?=-1 THEN '' ELSE lock_request_id END,updated_at=CURRENT_TIMESTAMP WHERE id=?`, enabled, enabled, enabled, id)
	if err != nil {
		return err
	}
	if affected, _ := result.RowsAffected(); affected == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *Store) DeleteMailAccount(ctx context.Context, id int64) error {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, `DELETE FROM mail_account_dispatches WHERE mail_account_id=?`, id); err != nil {
		return err
	}
	result, err := tx.ExecContext(ctx, `DELETE FROM mail_accounts WHERE id=?`, id)
	if err != nil {
		return err
	}
	if affected, _ := result.RowsAffected(); affected == 0 {
		return ErrNotFound
	}
	return tx.Commit()
}

func ParseMailAccountLine(line, selectedPlatform string) (string, string, string, error) {
	line = strings.TrimSpace(line)
	selectedPlatform = strings.TrimSpace(selectedPlatform)
	if mail, password, found := strings.Cut(line, "----"); found {
		mail = strings.TrimSpace(mail)
		password = strings.TrimSpace(password)
		if mail == "" || password == "" || selectedPlatform == "" {
			return "", "", "", fmt.Errorf("expected mail----password with selected platform")
		}
		return mail, password, selectedPlatform, nil
	}
	parts := strings.Split(line, "|")
	if len(parts) == 3 {
		mail, password, platform := strings.TrimSpace(parts[0]), strings.TrimSpace(parts[1]), strings.TrimSpace(parts[2])
		if mail != "" && password != "" && platform != "" {
			return mail, password, platform, nil
		}
	}
	return "", "", "", fmt.Errorf("expected mail----password with selected platform or mail|password|platform")
}
