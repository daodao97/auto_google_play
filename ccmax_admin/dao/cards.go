package dao

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"
)

var digits = regexp.MustCompile(`^\d+$`)

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
}

type CardStats struct {
	Available int `json:"available"`
	Cooling   int `json:"cooling"`
	Total     int `json:"total"`
}

const cardColumns = `id,source,card_id,card_no,expire_mmyy,ccv,usage_count,status,created_at,updated_at,last_dispatched_at,cooldown_until`

func scanCard(row interface{ Scan(...any) error }) (*Card, error) {
	c := &Card{}
	err := row.Scan(&c.ID, &c.Source, &c.CardID, &c.CardNo, &c.ExpireMMYY, &c.CCV, &c.UsageCount, &c.Status, &c.CreatedAt, &c.UpdatedAt, &c.LastDispatchedAt, &c.CooldownUntil)
	return c, err
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
	r, err := s.DB.ExecContext(ctx, `UPDATE card_pool SET source=?,card_id=?,card_no=?,expire_mmyy=?,ccv=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?`, strings.TrimSpace(c.Source), strings.TrimSpace(c.CardID), strings.TrimSpace(c.CardNo), strings.TrimSpace(c.ExpireMMYY), strings.TrimSpace(c.CCV), c.Status, id)
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
	r, err := s.DB.ExecContext(ctx, `UPDATE card_pool SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?`, status, id)
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
	stats := CardStats{}
	now := time.Now()
	err := s.DB.QueryRowContext(ctx, `SELECT
		COALESCE(SUM(CASE WHEN status=1 AND (cooldown_until IS NULL OR cooldown_until<=?) THEN 1 ELSE 0 END),0),
		COALESCE(SUM(CASE WHEN status=1 AND cooldown_until>? THEN 1 ELSE 0 END),0),
		count(*) FROM card_pool`, now, now).Scan(&stats.Available, &stats.Cooling, &stats.Total)
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

func (s *Store) DispatchCards(ctx context.Context, apiKeyID int64, requestID, source string, count int, ip string) ([]Card, error) {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	rows, err := tx.QueryContext(ctx, `SELECT `+qualifiedCardColumns("c")+` FROM card_dispatches d JOIN card_pool c ON c.id=d.card_pool_id WHERE d.request_id=? AND d.api_key_id=? ORDER BY d.id`, requestID, apiKeyID)
	if err != nil {
		return nil, err
	}
	existing := []Card{}
	for rows.Next() {
		card, scanErr := scanCard(rows)
		if scanErr != nil {
			rows.Close()
			return nil, scanErr
		}
		existing = append(existing, *card)
	}
	rows.Close()
	if len(existing) > 0 {
		if err := tx.Commit(); err != nil {
			return nil, err
		}
		return existing, nil
	}
	now := time.Now()
	where := ` WHERE status=1 AND (cooldown_until IS NULL OR cooldown_until<=?)`
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
	for i := range items {
		if _, err = tx.ExecContext(ctx, `UPDATE card_pool SET last_dispatched_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?`, now, items[i].ID); err != nil {
			return nil, err
		}
		if _, err = tx.ExecContext(ctx, `INSERT INTO card_dispatches(request_id,api_key_id,card_pool_id,client_ip) VALUES(?,?,?,?)`, requestID, apiKeyID, items[i].ID, ip); err != nil {
			return nil, err
		}
		items[i].LastDispatchedAt = &now
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return items, nil
}

func (s *Store) ReportCardUnavailable(ctx context.Context, apiKeyID int64, requestID string, cardPoolID int64) (*Card, error) {
	requestID = strings.TrimSpace(requestID)
	if requestID == "" || cardPoolID <= 0 {
		return nil, errors.New("requestId and positive cardPoolId are required")
	}
	var exists int
	err := s.DB.QueryRowContext(ctx, `SELECT 1 FROM card_dispatches WHERE api_key_id=? AND request_id=? AND card_pool_id=?`, apiKeyID, requestID, cardPoolID).Scan(&exists)
	if err != nil {
		return nil, err
	}
	if _, err = s.DB.ExecContext(ctx, `UPDATE card_pool SET status=-1,updated_at=CURRENT_TIMESTAMP WHERE id=?`, cardPoolID); err != nil {
		return nil, err
	}
	return scanCard(s.DB.QueryRowContext(ctx, `SELECT `+cardColumns+` FROM card_pool WHERE id=?`, cardPoolID))
}
func (s *Store) Credential(ctx context.Context, source string) (string, error) {
	var token string
	err := s.DB.QueryRowContext(ctx, `SELECT token FROM channel_credentials WHERE source=? COLLATE NOCASE AND status=1`, strings.TrimSpace(source)).Scan(&token)
	return token, err
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
