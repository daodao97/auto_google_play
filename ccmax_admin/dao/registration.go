package dao

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"time"
)

type RegistrationRun struct {
	ID             int64
	UpstreamRunID  string
	Status         string
	Platform       string
	RequestedCount int
	ImportedCount  int
	ConsumedCount  int
	LockID         string
	SummaryJSON    string
	TasksJSON      string
	Error          string
	StartedAt      time.Time
	FinishedAt     *time.Time
}

type RegistrationSchedule struct {
	Enabled       bool      `json:"enabled"`
	Platform      string    `json:"platform"`
	Count         int       `json:"count"`
	Concurrency   int       `json:"concurrency"`
	RetryMax      int       `json:"retryMax"`
	ProxyMode     string    `json:"proxyMode"`
	ProxyTemplate string    `json:"proxyTemplate"`
	MailFastPath  bool      `json:"mailFastPath"`
	UpdatedAt     time.Time `json:"updatedAt"`
	UpdatedBy     *int64    `json:"updatedBy,omitempty"`
}

func (s *Store) RegistrationSchedule(ctx context.Context) (*RegistrationSchedule, error) {
	item := &RegistrationSchedule{}
	var enabled, mailFastPath int
	err := s.DB.QueryRowContext(ctx, `SELECT enabled,platform,account_count,concurrency,retry_max,proxy_mode,proxy_template,mail_fast_path,updated_at,updated_by FROM registration_schedule WHERE id=1`).Scan(
		&enabled, &item.Platform, &item.Count, &item.Concurrency, &item.RetryMax, &item.ProxyMode, &item.ProxyTemplate, &mailFastPath, &item.UpdatedAt, &item.UpdatedBy,
	)
	item.Enabled = enabled == 1
	item.MailFastPath = mailFastPath == 1
	return item, err
}

func (s *Store) SetRegistrationSchedule(ctx context.Context, item RegistrationSchedule, adminID int64) error {
	enabled := -1
	if item.Enabled {
		enabled = 1
	}
	mailFastPath := 0
	if item.MailFastPath {
		mailFastPath = 1
	}
	_, err := s.DB.ExecContext(ctx, `UPDATE registration_schedule SET enabled=?,platform=?,account_count=?,concurrency=?,retry_max=?,proxy_mode=?,proxy_template=?,mail_fast_path=?,updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=1`, enabled, strings.ToLower(strings.TrimSpace(item.Platform)), item.Count, item.Concurrency, item.RetryMax, item.ProxyMode, strings.TrimSpace(item.ProxyTemplate), mailFastPath, adminID)
	return err
}

func (s *Store) AvailableMailAccountCount(ctx context.Context, platform string) (int, error) {
	if _, err := s.ReleaseExpiredMailAccountLeases(ctx); err != nil {
		return 0, err
	}
	var count int
	err := s.DB.QueryRowContext(ctx, `SELECT count(*) FROM mail_accounts WHERE enabled=1 AND status='unused' AND locked_until IS NULL AND platform=? COLLATE NOCASE`, strings.TrimSpace(platform)).Scan(&count)
	return count, err
}

func (s *Store) CreateRegistrationRun(ctx context.Context, run RegistrationRun, adminID int64, accounts []MailAccount) (int64, error) {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	result, err := tx.ExecContext(ctx, `INSERT INTO registration_runs(upstream_run_id,status,platform,requested_count,lock_id,created_by,started_at) VALUES(?,'running',?,?,?,?,?)`, run.UpstreamRunID, run.Platform, run.RequestedCount, run.LockID, adminID, run.StartedAt)
	if err != nil {
		return 0, err
	}
	id, err := result.LastInsertId()
	if err != nil {
		return 0, err
	}
	for _, account := range accounts {
		if _, err = tx.ExecContext(ctx, `INSERT INTO registration_run_accounts(registration_run_id,mail_account_id) VALUES(?,?)`, id, account.ID); err != nil {
			return 0, err
		}
	}
	return id, tx.Commit()
}

func (s *Store) UpdateRegistrationRunProgress(ctx context.Context, id int64, summary, tasks any, errorText string) error {
	summaryJSON, err := json.Marshal(summary)
	if err != nil {
		return err
	}
	tasksJSON, err := json.Marshal(tasks)
	if err != nil {
		return err
	}
	_, err = s.DB.ExecContext(ctx, `UPDATE registration_runs SET summary_json=?,tasks_json=?,error=? WHERE id=? AND status='running'`, string(summaryJSON), string(tasksJSON), errorText, id)
	return err
}

func (s *Store) FinishRegistrationRun(ctx context.Context, id int64, status string, imported, consumed int, errorText string) error {
	if status != "completed" && status != "failed" {
		return errors.New("registration status must be completed or failed")
	}
	_, err := s.DB.ExecContext(ctx, `UPDATE registration_runs SET status=?,imported_count=?,consumed_count=?,error=?,finished_at=CURRENT_TIMESTAMP WHERE id=?`, status, imported, consumed, errorText, id)
	return err
}

func (s *Store) LatestRegistrationRun(ctx context.Context) (*RegistrationRun, []MailAccount, error) {
	run := &RegistrationRun{}
	err := s.DB.QueryRowContext(ctx, `SELECT id,upstream_run_id,status,platform,requested_count,imported_count,consumed_count,lock_id,summary_json,tasks_json,error,started_at,finished_at FROM registration_runs ORDER BY id DESC LIMIT 1`).Scan(
		&run.ID, &run.UpstreamRunID, &run.Status, &run.Platform, &run.RequestedCount, &run.ImportedCount, &run.ConsumedCount, &run.LockID, &run.SummaryJSON, &run.TasksJSON, &run.Error, &run.StartedAt, &run.FinishedAt,
	)
	if err != nil {
		return nil, nil, err
	}
	rows, err := s.DB.QueryContext(ctx, `SELECT `+mailAccountColumns+` FROM registration_run_accounts r JOIN mail_accounts m ON m.id=r.mail_account_id LEFT JOIN claude_accounts c ON c.id=m.claude_account_id WHERE r.registration_run_id=? ORDER BY m.id`, run.ID)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	accounts := []MailAccount{}
	for rows.Next() {
		account, scanErr := scanMailAccount(rows)
		if scanErr != nil {
			return nil, nil, scanErr
		}
		accounts = append(accounts, *account)
	}
	return run, accounts, rows.Err()
}

func (s *Store) ReserveMailAccountsForRegistration(ctx context.Context, platform string, limit int, lockID string, lease time.Duration) ([]MailAccount, error) {
	platform = strings.ToLower(strings.TrimSpace(platform))
	lockID = strings.TrimSpace(lockID)
	if platform == "" || limit < 1 || limit > 200 || lockID == "" {
		return nil, errors.New("platform, count between 1 and 200, and lock ID are required")
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, `UPDATE mail_accounts SET locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE status='unused' AND locked_until IS NOT NULL AND locked_until<=unixepoch()`); err != nil {
		return nil, err
	}
	rows, err := tx.QueryContext(ctx, `SELECT `+mailAccountColumns+` FROM mail_accounts m LEFT JOIN claude_accounts c ON c.id=m.claude_account_id WHERE m.enabled=1 AND m.status='unused' AND m.locked_until IS NULL AND m.platform=? ORDER BY m.last_dispatched_at ASC,m.id ASC LIMIT ?`, platform, limit)
	if err != nil {
		return nil, err
	}
	items := []MailAccount{}
	for rows.Next() {
		item, scanErr := scanMailAccount(rows)
		if scanErr != nil {
			rows.Close()
			return nil, scanErr
		}
		items = append(items, *item)
	}
	if err = rows.Close(); err != nil {
		return nil, err
	}
	if len(items) == 0 {
		return nil, errors.New("insufficient mail accounts")
	}
	if len(items) < limit {
		return nil, errors.New("insufficient mail accounts for requested registration count")
	}
	lockedUntil := time.Now().Add(lease).Unix()
	for index := range items {
		result, updateErr := tx.ExecContext(ctx, `UPDATE mail_accounts SET locked_until=?,lock_request_id=?,dispatch_count=dispatch_count+1,last_dispatched_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=? AND enabled=1 AND status='unused' AND locked_until IS NULL`, lockedUntil, lockID, items[index].ID)
		if updateErr != nil {
			return nil, updateErr
		}
		if affected, _ := result.RowsAffected(); affected != 1 {
			return nil, errors.New("mail account allocation conflict; please retry")
		}
		value := time.Unix(lockedUntil, 0)
		items[index].LockedUntil = &value
		items[index].LockRequestID = lockID
		items[index].DispatchCount++
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return items, nil
}

func (s *Store) ReleaseMailRegistration(ctx context.Context, lockID string) error {
	_, err := s.DB.ExecContext(ctx, `UPDATE mail_accounts SET locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE status='unused' AND lock_request_id=?`, strings.TrimSpace(lockID))
	return err
}

func (s *Store) ConsumeMailRegistration(ctx context.Context, mailAccountID int64, lockID string) error {
	result, err := s.DB.ExecContext(ctx, `UPDATE mail_accounts SET status='used',used_at=CURRENT_TIMESTAMP,locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='unused' AND lock_request_id=?`, mailAccountID, strings.TrimSpace(lockID))
	if err != nil {
		return err
	}
	if affected, _ := result.RowsAffected(); affected == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *Store) ImportRegisteredClaudeAccount(ctx context.Context, mailAccountID int64, lockID, sessionKey string) (int64, bool, error) {
	sessionKey = strings.TrimSpace(sessionKey)
	if mailAccountID <= 0 || strings.TrimSpace(lockID) == "" || sessionKey == "" {
		return 0, false, errors.New("mail account, registration lock and session key are required")
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return 0, false, err
	}
	defer tx.Rollback()
	var mail, password, status, currentLock string
	var linkedID sql.NullInt64
	if err = tx.QueryRowContext(ctx, `SELECT mail,password,status,lock_request_id,claude_account_id FROM mail_accounts WHERE id=?`, mailAccountID).Scan(&mail, &password, &status, &currentLock, &linkedID); err != nil {
		return 0, false, err
	}
	if status == "used" && linkedID.Valid {
		return linkedID.Int64, false, tx.Commit()
	}
	if status != "unused" || currentLock != strings.TrimSpace(lockID) {
		return 0, false, errors.New("mail account registration lease has expired or changed")
	}
	var claudeID int64
	created := false
	err = tx.QueryRowContext(ctx, `SELECT id FROM claude_accounts WHERE mail=? COLLATE NOCASE`, mail).Scan(&claudeID)
	if errors.Is(err, sql.ErrNoRows) {
		result, insertErr := tx.ExecContext(ctx, `INSERT INTO claude_accounts(mail,password,session_key,plan,status) VALUES(?,?,?,'free',1)`, strings.ToLower(mail), password, sessionKey)
		if insertErr != nil {
			return 0, false, insertErr
		}
		claudeID, err = result.LastInsertId()
		created = true
	} else if err != nil {
		return 0, false, err
	}
	if _, err = tx.ExecContext(ctx, `UPDATE mail_accounts SET status='used',claude_account_id=?,used_at=CURRENT_TIMESTAMP,locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE id=?`, claudeID, mailAccountID); err != nil {
		return 0, false, err
	}
	if err = tx.Commit(); err != nil {
		return 0, false, err
	}
	return claudeID, created, nil
}
