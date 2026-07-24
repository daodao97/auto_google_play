package dao

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"
)

var digits = regexp.MustCompile(`^\d+$`)

const CardCooldown = 5 * time.Hour

type Card struct {
	ID               int64      `json:"id"`
	Source           string     `json:"source"`
	CardID           string     `json:"cardId"`
	CardNo           string     `json:"cardNo"`
	ExpireMMYY       string     `json:"expireMmyy"`
	CCV              string     `json:"ccv"`
	UsageCount       int        `json:"usageCount"`
	Status           int        `json:"status"`
	CreatedAt        time.Time  `json:"createdAt"`
	UpdatedAt        time.Time  `json:"updatedAt"`
	LastDispatchedAt *time.Time `json:"lastDispatchedAt"`
	CooldownUntil    *time.Time `json:"cooldownUntil"`
	LockedUntil      *time.Time `json:"lockedUntil"`
	LockRequestID    string     `json:"lockRequestId,omitempty"`
}

type CardStats struct {
	Available int `json:"available"`
	Cooling   int `json:"cooling"`
	Locked    int `json:"locked"`
	Total     int `json:"total"`
}

const cardColumns = `id,source,card_id,card_no,expire_mmyy,ccv,usage_count,status,created_at,updated_at,last_dispatched_at,cooldown_until,locked_until,lock_request_id`

type cardScanner interface{ Scan(...any) error }

func cardScanTargets(card *Card, lockedUntil *sql.NullInt64) []any {
	return []any{
		&card.ID, &card.Source, &card.CardID, &card.CardNo, &card.ExpireMMYY, &card.CCV,
		&card.UsageCount, &card.Status, &card.CreatedAt, &card.UpdatedAt, &card.LastDispatchedAt,
		&card.CooldownUntil, lockedUntil, &card.LockRequestID,
	}
}

func applyCardLock(card *Card, lockedUntil sql.NullInt64) {
	if lockedUntil.Valid {
		value := time.Unix(lockedUntil.Int64, 0)
		card.LockedUntil = &value
	}
}

func scanCard(row cardScanner) (*Card, error) {
	c := &Card{}
	var lockedUntil sql.NullInt64
	err := row.Scan(cardScanTargets(c, &lockedUntil)...)
	if err == nil {
		applyCardLock(c, lockedUntil)
	}
	return c, err
}

func scanDispatchedCard(row cardScanner) (*Card, string, error) {
	card := &Card{}
	var lockedUntil sql.NullInt64
	var reportStatus string
	targets := append(cardScanTargets(card, &lockedUntil), &reportStatus)
	err := row.Scan(targets...)
	if err == nil {
		applyCardLock(card, lockedUntil)
	}
	return card, reportStatus, err
}
func validateCard(c Card) error {
	c.Source = strings.TrimSpace(c.Source)
	c.CardNo = strings.TrimSpace(c.CardNo)
	c.ExpireMMYY = strings.TrimSpace(c.ExpireMMYY)
	c.CCV = strings.TrimSpace(c.CCV)
	if c.Source == "" || c.CardNo == "" || c.ExpireMMYY == "" || c.CCV == "" {
		return errors.New("source, cardNo, expireMmyy and ccv are required")
	}
	if len(c.ExpireMMYY) != 4 || !digits.MatchString(c.ExpireMMYY) {
		return errors.New("expireMmyy must be 4 digits")
	}
	if len(c.CCV) < 3 || len(c.CCV) > 4 || !digits.MatchString(c.CCV) {
		return errors.New("ccv must be 3 or 4 digits")
	}
	return nil
}

func normalizeExpireMMYY(value string) string {
	return strings.ReplaceAll(strings.TrimSpace(value), "/", "")
}

func (s *Store) CreateCard(ctx context.Context, c Card) (int64, error) {
	c.ExpireMMYY = normalizeExpireMMYY(c.ExpireMMYY)
	if err := validateCard(c); err != nil {
		return 0, err
	}
	status := c.Status
	if status == 0 {
		status = 1
	}
	r, err := s.DB.ExecContext(ctx, `INSERT INTO card_pool(source,card_id,card_no,expire_mmyy,ccv,status) VALUES(?,?,?,?,?,?)`, strings.TrimSpace(c.Source), strings.TrimSpace(c.CardID), strings.TrimSpace(c.CardNo), strings.TrimSpace(c.ExpireMMYY), strings.TrimSpace(c.CCV), status)
	if err != nil {
		return 0, err
	}
	return r.LastInsertId()
}
func (s *Store) UpdateCard(ctx context.Context, id int64, c Card) error {
	c.ExpireMMYY = normalizeExpireMMYY(c.ExpireMMYY)
	if err := validateCard(c); err != nil {
		return err
	}
	r, err := s.DB.ExecContext(ctx, `UPDATE card_pool SET source=?,card_id=?,card_no=?,expire_mmyy=?,ccv=?,status=?,locked_until=CASE WHEN ?=-1 THEN NULL ELSE locked_until END,lock_request_id=CASE WHEN ?=-1 THEN '' ELSE lock_request_id END,updated_at=CURRENT_TIMESTAMP WHERE id=?`, strings.TrimSpace(c.Source), strings.TrimSpace(c.CardID), strings.TrimSpace(c.CardNo), strings.TrimSpace(c.ExpireMMYY), strings.TrimSpace(c.CCV), c.Status, c.Status, c.Status, id)
	if err != nil {
		return err
	}
	n, _ := r.RowsAffected()
	if n == 0 {
		return ErrNotFound
	}
	return nil
}
func (s *Store) SetCardStatus(ctx context.Context, id int64, status int) error {
	if status != 1 && status != -1 {
		return errors.New("status must be 1 or -1")
	}
	r, err := s.DB.ExecContext(ctx, `UPDATE card_pool SET status=?,locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE id=?`, status, id)
	if err != nil {
		return err
	}
	n, _ := r.RowsAffected()
	if n == 0 {
		return ErrNotFound
	}
	return nil
}
func (s *Store) ListCards(ctx context.Context, page, size int, query, source string, status int) ([]Card, int, error) {
	if _, err := s.ReleaseExpiredCardLeases(ctx); err != nil {
		return nil, 0, err
	}
	where := ` WHERE 1=1`
	args := []any{}
	if query != "" {
		where += ` AND card_no LIKE ?`
		args = append(args, "%"+strings.TrimSpace(query)+"%")
	}
	if source != "" {
		where += ` AND source=?`
		args = append(args, strings.TrimSpace(source))
	}
	if status != 0 {
		where += ` AND status=?`
		args = append(args, status)
	}
	var total int
	if err := s.DB.QueryRowContext(ctx, `SELECT count(*) FROM card_pool`+where, args...).Scan(&total); err != nil {
		return nil, 0, err
	}
	args = append(args, size, (page-1)*size)
	rows, err := s.DB.QueryContext(ctx, `SELECT `+cardColumns+` FROM card_pool`+where+` ORDER BY id DESC LIMIT ? OFFSET ?`, args...)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	items := []Card{}
	for rows.Next() {
		c, e := scanCard(rows)
		if e != nil {
			return nil, 0, e
		}
		items = append(items, *c)
	}
	return items, total, rows.Err()
}

func (s *Store) CardStats(ctx context.Context) (CardStats, error) {
	if _, err := s.ReleaseExpiredCardLeases(ctx); err != nil {
		return CardStats{}, err
	}
	stats := CardStats{}
	now := time.Now()
	nowUnix := now.Unix()
	err := s.DB.QueryRowContext(ctx, `SELECT
		COALESCE(SUM(CASE WHEN status=1 AND (cooldown_until IS NULL OR cooldown_until<=?) AND (locked_until IS NULL OR locked_until<=?) THEN 1 ELSE 0 END),0),
		COALESCE(SUM(CASE WHEN status=1 AND cooldown_until>? THEN 1 ELSE 0 END),0),
		COALESCE(SUM(CASE WHEN status=1 AND locked_until>? THEN 1 ELSE 0 END),0),
		count(*) FROM card_pool`, now, nowUnix, now, nowUnix).Scan(&stats.Available, &stats.Cooling, &stats.Locked, &stats.Total)
	return stats, err
}
func qualifiedCardColumns(alias string) string {
	return alias + "." + strings.ReplaceAll(cardColumns, ",", ","+alias+".")
}

func (s *Store) CardByID(ctx context.Context, id int64) (*Card, error) {
	return scanCard(s.DB.QueryRowContext(ctx, `SELECT `+cardColumns+` FROM card_pool WHERE id=?`, id))
}

func (s *Store) CardByIDForAPIKey(ctx context.Context, id, apiKeyID int64) (*Card, error) {
	return scanCard(s.DB.QueryRowContext(ctx, `SELECT `+qualifiedCardColumns("c")+` FROM card_pool c WHERE c.id=? AND c.status=1 AND EXISTS(SELECT 1 FROM card_dispatches d WHERE d.card_pool_id=c.id AND d.api_key_id=?)`, id, apiKeyID))
}

func (s *Store) DispatchCards(ctx context.Context, apiKeyID int64, requestID, source string, count int, lease time.Duration, ip string) ([]Card, error) {
	requestID = strings.TrimSpace(requestID)
	if requestID == "" || count <= 0 || lease <= 0 {
		return nil, errors.New("requestId, positive count and lease are required")
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, `UPDATE card_pool SET locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE locked_until IS NOT NULL AND locked_until<=unixepoch()`); err != nil {
		return nil, err
	}
	now := time.Now()
	rows, err := tx.QueryContext(ctx, `SELECT `+qualifiedCardColumns("c")+`,d.report_status FROM card_dispatches d JOIN card_pool c ON c.id=d.card_pool_id WHERE d.request_id=? AND d.api_key_id=? ORDER BY d.id`, requestID, apiKeyID)
	if err != nil {
		return nil, err
	}
	existing := []Card{}
	expiredLease := false
	for rows.Next() {
		card, reportStatus, scanErr := scanDispatchedCard(rows)
		if scanErr != nil {
			rows.Close()
			return nil, scanErr
		}
		if reportStatus == "" && (card.LockRequestID != requestID || card.LockedUntil == nil || !card.LockedUntil.After(now)) {
			expiredLease = true
		}
		existing = append(existing, *card)
	}
	if err = rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	rows.Close()
	if len(existing) > 0 {
		if expiredLease {
			return nil, errors.New("card idempotency lease has expired")
		}
		if err := tx.Commit(); err != nil {
			return nil, err
		}
		return existing, nil
	}
	where := ` WHERE status=1 AND (cooldown_until IS NULL OR cooldown_until<=?) AND locked_until IS NULL`
	args := []any{now}
	if strings.TrimSpace(source) != "" {
		where += ` AND source=? COLLATE NOCASE`
		args = append(args, strings.TrimSpace(source))
	}
	args = append(args, count)
	rows, err = tx.QueryContext(ctx, `SELECT `+cardColumns+` FROM card_pool`+where+` ORDER BY usage_count ASC,last_dispatched_at ASC,id ASC LIMIT ?`, args...)
	if err != nil {
		return nil, err
	}
	items := []Card{}
	for rows.Next() {
		card, scanErr := scanCard(rows)
		if scanErr != nil {
			rows.Close()
			return nil, scanErr
		}
		items = append(items, *card)
	}
	rows.Close()
	if len(items) != count {
		return nil, fmt.Errorf("insufficient cards: available=%d requested=%d", len(items), count)
	}
	lockedUntil := now.Add(lease).Truncate(time.Second)
	for i := range items {
		result, updateErr := tx.ExecContext(ctx, `UPDATE card_pool SET last_dispatched_at=?,locked_until=?,lock_request_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status=1 AND locked_until IS NULL AND (cooldown_until IS NULL OR cooldown_until<=?)`, now, lockedUntil.Unix(), requestID, items[i].ID, now)
		if updateErr != nil {
			return nil, updateErr
		}
		if affected, _ := result.RowsAffected(); affected != 1 {
			return nil, errors.New("card allocation conflict; please retry")
		}
		if _, err = tx.ExecContext(ctx, `INSERT INTO card_dispatches(request_id,api_key_id,card_pool_id,client_ip) VALUES(?,?,?,?)`, requestID, apiKeyID, items[i].ID, ip); err != nil {
			return nil, err
		}
		items[i].LastDispatchedAt = &now
		items[i].LockedUntil = &lockedUntil
		items[i].LockRequestID = requestID
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return items, nil
}

func (s *Store) ReportCard(ctx context.Context, apiKeyID int64, requestID string, cardPoolID int64, status, reason string) (*Card, error) {
	requestID = strings.TrimSpace(requestID)
	status = strings.ToLower(strings.TrimSpace(status))
	reason = strings.TrimSpace(reason)
	if requestID == "" || cardPoolID <= 0 {
		return nil, errors.New("requestId and positive cardPoolId are required")
	}
	if status != "used" && status != "unavailable" {
		return nil, errors.New("status must be used or unavailable")
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, `UPDATE card_pool SET locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE locked_until IS NOT NULL AND locked_until<=unixepoch()`); err != nil {
		return nil, err
	}
	var reportedStatus string
	if err = tx.QueryRowContext(ctx, `SELECT report_status FROM card_dispatches WHERE api_key_id=? AND request_id=? AND card_pool_id=?`, apiKeyID, requestID, cardPoolID).Scan(&reportedStatus); err != nil {
		return nil, err
	}
	if reportedStatus != "" && reportedStatus != status {
		return nil, fmt.Errorf("card was already reported as %s", reportedStatus)
	}
	if reportedStatus == "" {
		var lockRequestID string
		var lockedUntil sql.NullInt64
		if err = tx.QueryRowContext(ctx, `SELECT lock_request_id,locked_until FROM card_pool WHERE id=?`, cardPoolID).Scan(&lockRequestID, &lockedUntil); err != nil {
			return nil, err
		}
		if lockRequestID != requestID || !lockedUntil.Valid || lockedUntil.Int64 <= time.Now().Unix() {
			return nil, errors.New("card lease has expired or been reassigned")
		}
		result, updateErr := tx.ExecContext(ctx, `UPDATE card_dispatches SET report_status=?,report_reason=?,reported_at=CURRENT_TIMESTAMP WHERE api_key_id=? AND request_id=? AND card_pool_id=? AND report_status=''`, status, reason, apiKeyID, requestID, cardPoolID)
		if updateErr != nil {
			return nil, updateErr
		}
		if affected, _ := result.RowsAffected(); affected != 1 {
			return nil, errors.New("card report conflict; please retry")
		}
		switch status {
		case "used":
			cooldownUntil := time.Now().Add(CardCooldown)
			if _, err = tx.ExecContext(ctx, `UPDATE card_pool SET usage_count=usage_count+1,cooldown_until=CASE WHEN cooldown_until IS NULL OR cooldown_until<? THEN ? ELSE cooldown_until END,locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE id=?`, cooldownUntil, cooldownUntil, cardPoolID); err != nil {
				return nil, err
			}
		case "unavailable":
			if _, err = tx.ExecContext(ctx, `UPDATE card_pool SET status=-1,locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE id=?`, cardPoolID); err != nil {
				return nil, err
			}
		}
	}
	card, err := scanCard(tx.QueryRowContext(ctx, `SELECT `+cardColumns+` FROM card_pool WHERE id=?`, cardPoolID))
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return card, nil
}

func (s *Store) ReleaseExpiredCardLeases(ctx context.Context) (int64, error) {
	result, err := s.DB.ExecContext(ctx, `UPDATE card_pool SET locked_until=NULL,lock_request_id='',updated_at=CURRENT_TIMESTAMP WHERE locked_until IS NOT NULL AND locked_until<=unixepoch()`)
	if err != nil {
		return 0, err
	}
	return result.RowsAffected()
}

func (s *Store) ReportCardUnavailable(ctx context.Context, apiKeyID int64, requestID string, cardPoolID int64) (*Card, error) {
	return s.ReportCard(ctx, apiKeyID, requestID, cardPoolID, "unavailable", "")
}
func (s *Store) Credential(ctx context.Context, source string) (string, error) {
	var token string
	err := s.DB.QueryRowContext(ctx, `SELECT token FROM channel_credentials WHERE source=? COLLATE NOCASE AND status=1`, strings.TrimSpace(source)).Scan(&token)
	return token, err
}

func (s *Store) CredentialSourceByPrefix(ctx context.Context, prefix string) (string, error) {
	prefix = strings.ToLower(strings.TrimSpace(prefix))
	if prefix == "" {
		return "", errors.New("credential source prefix is required")
	}
	var source string
	err := s.DB.QueryRowContext(ctx, `SELECT source FROM channel_credentials WHERE status=1 AND lower(source) LIKE ? ORDER BY CASE WHEN lower(source)=? THEN 0 ELSE 1 END,source LIMIT 1`, prefix+"%", prefix).Scan(&source)
	return source, err
}

func (s *Store) SetCredential(ctx context.Context, source, token string) error {
	source = strings.ToLower(strings.TrimSpace(source))
	token = strings.TrimSpace(token)
	if source == "" || token == "" {
		return errors.New("source and token are required")
	}
	_, err := s.DB.ExecContext(ctx, `INSERT INTO channel_credentials(source,token) VALUES(?,?) ON CONFLICT(source) DO UPDATE SET token=excluded.token,status=1,updated_at=CURRENT_TIMESTAMP`, source, token)
	return err
}
func (s *Store) ListCredentials(ctx context.Context) ([]map[string]any, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT source,status,created_at,updated_at FROM channel_credentials ORDER BY source`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var source string
		var status int
		var created, updated time.Time
		if err := rows.Scan(&source, &status, &created, &updated); err != nil {
			return nil, err
		}
		items = append(items, map[string]any{"source": source, "status": status, "tokenConfigured": true, "createdAt": created, "updatedAt": updated})
	}
	return items, rows.Err()
}
