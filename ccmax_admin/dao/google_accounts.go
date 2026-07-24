package dao

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"
)

type GoogleAccount struct {
	ID               int64      `json:"id"`
	Mail             string     `json:"mail"`
	Password         string     `json:"password,omitempty"`
	Status           string     `json:"status"`
	Enabled          int        `json:"enabled"`
	DispatchCount    int        `json:"dispatchCount"`
	LastDispatchedAt *time.Time `json:"lastDispatchedAt"`
	LockedUntil      *time.Time `json:"lockedUntil"`
	LockRequestID    string     `json:"lockRequestId,omitempty"`
	ReportedAt       *time.Time `json:"reportedAt"`
	CreatedAt        time.Time  `json:"createdAt"`
}

const googleAccountColumns = `g.id,g.mail,g.password,CASE WHEN g.status='unused' THEN 'unused' ELSE COALESCE(NULLIF(g.report_status,''),'used') END,g.enabled,g.dispatch_count,g.last_dispatched_at,g.locked_until,g.lock_request_id,g.used_at,g.created_at`

func scanGoogleAccount(row interface{ Scan(...any) error }) (*GoogleAccount, error) {
	item := &GoogleAccount{}
	var lockedUntil sql.NullInt64
	err := row.Scan(&item.ID, &item.Mail, &item.Password, &item.Status, &item.Enabled, &item.DispatchCount, &item.LastDispatchedAt, &lockedUntil, &item.LockRequestID, &item.ReportedAt, &item.CreatedAt)
	if err == nil && lockedUntil.Valid {
		value := time.Unix(lockedUntil.Int64, 0)
		item.LockedUntil = &value
	}
	return item, err
}

func (s *Store) CreateGoogleAccount(ctx context.Context, mail, password string) (int64, error) {
	mail = strings.ToLower(strings.TrimSpace(mail))
	if mail == "" || !strings.Contains(mail, "@") || password == "" {
		return 0, errors.New("valid mail and password are required")
	}
	if strings.ContainsAny(mail, "\r\n|") || strings.ContainsAny(password, "\r\n") {
		return 0, errors.New("mail and password must be single-line values")
	}
	result, err := s.DB.ExecContext(ctx, `INSERT INTO google_accounts(mail,password) VALUES(?,?)`, mail, password)
	if err != nil {
		return 0, err
	}
	return result.LastInsertId()
}

func (s *Store) ListGoogleAccounts(ctx context.Context, page, size int, query, status string, enabled int) ([]GoogleAccount, int, error) {
	if _, err := s.ReleaseExpiredGoogleAccountLeases(ctx); err != nil {
		return nil, 0, err
	}
	where := ` WHERE 1=1`
	args := []any{}
	if query = strings.TrimSpace(query); query != "" {
		where += ` AND g.mail LIKE ?`
		args = append(args, "%"+query+"%")
	}
	if status = strings.ToLower(strings.TrimSpace(status)); status != "" {
		if status != "unused" && status != "used" && status != "discarded" && status != "login_failed" {
			return nil, 0, errors.New("status must be unused, used, discarded or login_failed")
		}
		if status == "unused" {
			where += ` AND g.status='unused'`
		} else {
			where += ` AND g.status='used' AND COALESCE(NULLIF(g.report_status,''),'used')=?`
			args = append(args, status)
		}
	}
	if enabled != 0 {
		if enabled != 1 && enabled != -1 {
			return nil, 0, errors.New("enabled must be 1 or -1")
		}
		where += ` AND g.enabled=?`
		args = append(args, enabled)
	}
	var total int
	if err := s.DB.QueryRowContext(ctx, `SELECT count(*) FROM google_accounts g`+where, args...).Scan(&total); err != nil {
		return nil, 0, err
	}
	args = append(args, size, (page-1)*size)
	rows, err := s.DB.QueryContext(ctx, `SELECT `+googleAccountColumns+` FROM google_accounts g`+where+` ORDER BY g.id DESC LIMIT ? OFFSET ?`, args...)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	items := []GoogleAccount{}
	for rows.Next() {
		item, scanErr := scanGoogleAccount(rows)
		if scanErr != nil {
			return nil, 0, scanErr
		}
		items = append(items, *item)
	}
	return items, total, rows.Err()
}

func (s *Store) DispatchGoogleAccount(ctx context.Context, apiKeyID int64, requestID string, lease time.Duration, ip string) (*GoogleAccount, error) {
	requestID = strings.TrimSpace(requestID)
	if requestID == "" || lease <= 0 {
		return nil, errors.New("requestId and positive lease are required")
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, `UPDATE google_accounts SET locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE status='unused' AND locked_until IS NOT NULL AND locked_until<=unixepoch()`); err != nil {
		return nil, err
	}
	item, err := scanGoogleAccount(tx.QueryRowContext(ctx, `SELECT `+googleAccountColumns+` FROM google_account_dispatches d JOIN google_accounts g ON g.id=d.google_account_id WHERE d.api_key_id=? AND d.request_id=?`, apiKeyID, requestID))
	if err == nil {
		if item.Status != "unused" || (item.LockRequestID == requestID && item.LockedUntil != nil && item.LockedUntil.After(time.Now())) {
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
	item, err = scanGoogleAccount(tx.QueryRowContext(ctx, `SELECT `+googleAccountColumns+` FROM google_accounts g WHERE g.enabled=1 AND g.status='unused' AND g.locked_until IS NULL ORDER BY g.last_dispatched_at ASC,g.id ASC LIMIT 1`))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, errors.New("insufficient google accounts")
	}
	if err != nil {
		return nil, err
	}
	now := time.Now()
	lockedUntil := now.Add(lease).Truncate(time.Second)
	result, err := tx.ExecContext(ctx, `UPDATE google_accounts SET locked_until=?,lock_request_id=?,dispatch_count=dispatch_count+1,last_dispatched_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND enabled=1 AND status='unused' AND locked_until IS NULL`, lockedUntil.Unix(), requestID, now, item.ID)
	if err != nil {
		return nil, err
	}
	if affected, _ := result.RowsAffected(); affected != 1 {
		return nil, errors.New("google account allocation conflict; please retry")
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO google_account_dispatches(request_id,api_key_id,google_account_id,client_ip) VALUES(?,?,?,?)`, requestID, apiKeyID, item.ID, ip); err != nil {
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

func (s *Store) ReportGoogleAccount(ctx context.Context, apiKeyID int64, requestID string, googleAccountID int64, reportStatus string) (*GoogleAccount, error) {
	requestID = strings.TrimSpace(requestID)
	reportStatus = strings.ToLower(strings.TrimSpace(reportStatus))
	if requestID == "" || googleAccountID <= 0 || reportStatus == "" {
		return nil, errors.New("requestId, positive googleAccountId and status are required")
	}
	if reportStatus != "used" && reportStatus != "discarded" && reportStatus != "login_failed" {
		return nil, errors.New("status must be used, discarded or login_failed")
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, `UPDATE google_accounts SET locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE status='unused' AND locked_until IS NOT NULL AND locked_until<=unixepoch()`); err != nil {
		return nil, err
	}
	var dispatchedID int64
	if err = tx.QueryRowContext(ctx, `SELECT google_account_id FROM google_account_dispatches WHERE api_key_id=? AND request_id=?`, apiKeyID, requestID).Scan(&dispatchedID); err != nil {
		return nil, err
	}
	if dispatchedID != googleAccountID {
		return nil, errors.New("google account does not belong to this dispatch request")
	}
	var status, lockRequestID, existingReportStatus string
	var lockedUntil sql.NullInt64
	if err = tx.QueryRowContext(ctx, `SELECT status,lock_request_id,locked_until,COALESCE(NULLIF(report_status,''),'used') FROM google_accounts WHERE id=?`, googleAccountID).Scan(&status, &lockRequestID, &lockedUntil, &existingReportStatus); err != nil {
		return nil, err
	}
	if status == "used" {
		if existingReportStatus == reportStatus {
			item, scanErr := scanGoogleAccount(tx.QueryRowContext(ctx, `SELECT `+googleAccountColumns+` FROM google_accounts g WHERE g.id=?`, googleAccountID))
			if scanErr != nil {
				return nil, scanErr
			}
			if err = tx.Commit(); err != nil {
				return nil, err
			}
			return item, nil
		}
		return nil, fmt.Errorf("google account was already reported as %s", existingReportStatus)
	}
	if lockRequestID != requestID || !lockedUntil.Valid || lockedUntil.Int64 <= time.Now().Unix() {
		return nil, errors.New("google account lease has expired or been reassigned")
	}
	if _, err = tx.ExecContext(ctx, `UPDATE google_accounts SET status='used',report_status=?,claude_account_id=NULL,used_at=CURRENT_TIMESTAMP,locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='unused'`, reportStatus, googleAccountID); err != nil {
		return nil, err
	}
	item, err := scanGoogleAccount(tx.QueryRowContext(ctx, `SELECT `+googleAccountColumns+` FROM google_accounts g WHERE g.id=?`, googleAccountID))
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return item, nil
}

func (s *Store) ReleaseExpiredGoogleAccountLeases(ctx context.Context) (int64, error) {
	result, err := s.DB.ExecContext(ctx, `UPDATE google_accounts SET locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE status='unused' AND locked_until IS NOT NULL AND locked_until<=unixepoch()`)
	if err != nil {
		return 0, err
	}
	return result.RowsAffected()
}

func (s *Store) GoogleAccountStats(ctx context.Context) (map[string]int, error) {
	if _, err := s.ReleaseExpiredGoogleAccountLeases(ctx); err != nil {
		return nil, err
	}
	result := map[string]int{"unused": 0, "locked": 0, "used": 0, "discarded": 0, "login_failed": 0, "total": 0}
	rows, err := s.DB.QueryContext(ctx, `SELECT status,COALESCE(NULLIF(report_status,''),'used'),enabled,CASE WHEN status='unused' AND locked_until IS NOT NULL THEN 1 ELSE 0 END,count(*) FROM google_accounts GROUP BY status,report_status,enabled,CASE WHEN status='unused' AND locked_until IS NOT NULL THEN 1 ELSE 0 END`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var status, reportStatus string
		var enabled, locked, count int
		if err = rows.Scan(&status, &reportStatus, &enabled, &locked, &count); err != nil {
			return nil, err
		}
		result["total"] += count
		if status == "used" {
			result[reportStatus] += count
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

func (s *Store) SetGoogleAccountEnabled(ctx context.Context, id int64, enabled int) error {
	if enabled != 1 && enabled != -1 {
		return errors.New("enabled must be 1 or -1")
	}
	result, err := s.DB.ExecContext(ctx, `UPDATE google_accounts SET enabled=?,locked_until=CASE WHEN ?=-1 THEN NULL ELSE locked_until END,lock_request_id=CASE WHEN ?=-1 THEN '' ELSE lock_request_id END,updated_at=CURRENT_TIMESTAMP WHERE id=?`, enabled, enabled, enabled, id)
	if err != nil {
		return err
	}
	if affected, _ := result.RowsAffected(); affected == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *Store) DeleteGoogleAccount(ctx context.Context, id int64) error {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, `DELETE FROM google_account_dispatches WHERE google_account_id=?`, id); err != nil {
		return err
	}
	result, err := tx.ExecContext(ctx, `DELETE FROM google_accounts WHERE id=?`, id)
	if err != nil {
		return err
	}
	if affected, _ := result.RowsAffected(); affected == 0 {
		return ErrNotFound
	}
	return tx.Commit()
}

func ParseGoogleAccountLine(line string) (string, string, error) {
	line = strings.TrimSpace(line)
	mail, password, found := strings.Cut(line, "|")
	mail = strings.TrimSpace(mail)
	password = strings.TrimSpace(password)
	if !found || mail == "" || password == "" {
		return "", "", fmt.Errorf("expected mail|password")
	}
	return mail, password, nil
}
