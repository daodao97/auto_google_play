package dao

import (
	"context"
	"database/sql"
	"errors"
	"strings"
	"time"
)

type ChatGPTCDK struct {
	ID        int64      `json:"id"`
	Code      string     `json:"code"`
	SKU       string     `json:"sku"`
	Status    string     `json:"status"`
	Used      bool       `json:"used"`
	OrderNo   string     `json:"orderNo"`
	Remark    string     `json:"remark"`
	UsedAt    *time.Time `json:"usedAt"`
	TaskID    *int64     `json:"taskId"`
	CreatedAt time.Time  `json:"createdAt"`
	UpdatedAt time.Time  `json:"updatedAt"`
}

const chatGPTCDKColumns = `id,code,sku,status,(status='used'),order_no,remark,used_at,task_id,created_at,updated_at`
const qualifiedChatGPTCDKColumns = `c.id,c.code,c.sku,c.status,(c.status='used'),c.order_no,c.remark,c.used_at,c.task_id,c.created_at,c.updated_at`

func scanChatGPTCDK(row interface{ Scan(...any) error }) (*ChatGPTCDK, error) {
	item := &ChatGPTCDK{}
	err := row.Scan(&item.ID, &item.Code, &item.SKU, &item.Status, &item.Used, &item.OrderNo, &item.Remark, &item.UsedAt, &item.TaskID, &item.CreatedAt, &item.UpdatedAt)
	return item, err
}

func ValidChatGPTSKU(sku string) bool {
	switch strings.ToLower(strings.TrimSpace(sku)) {
	case "plus", "pro", "prolite":
		return true
	default:
		return false
	}
}

func (s *Store) CreateChatGPTCDKs(ctx context.Context, codes []string, sku, orderNo, remark string, adminID int64) ([]ChatGPTCDK, error) {
	sku = strings.ToLower(strings.TrimSpace(sku))
	if !ValidChatGPTSKU(sku) || len(codes) < 1 || len(codes) > 1000 {
		return nil, errors.New("sku must be plus, pro or prolite and quantity must be 1-1000")
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	items := make([]ChatGPTCDK, 0, len(codes))
	for _, code := range codes {
		code = strings.TrimSpace(code)
		if code == "" {
			return nil, errors.New("cdk code is required")
		}
		r, err := tx.ExecContext(ctx, `INSERT INTO chatgpt_cdks(code,sku,order_no,remark,created_by) VALUES(?,?,?,?,?)`, code, sku, strings.TrimSpace(orderNo), strings.TrimSpace(remark), adminID)
		if err != nil {
			return nil, err
		}
		id, err := r.LastInsertId()
		if err != nil {
			return nil, err
		}
		items = append(items, ChatGPTCDK{ID: id, Code: code, SKU: sku, Status: "available", OrderNo: strings.TrimSpace(orderNo), Remark: strings.TrimSpace(remark)})
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return items, nil
}

func (s *Store) ListChatGPTCDKs(ctx context.Context, page, size int, query, sku, status string) ([]ChatGPTCDK, int, error) {
	where, args := ` WHERE 1=1`, []any{}
	if q := strings.TrimSpace(query); q != "" {
		where += ` AND (code LIKE ? OR order_no LIKE ? OR remark LIKE ? OR EXISTS(SELECT 1 FROM chatgpt_tasks t WHERE t.id=chatgpt_cdks.task_id AND (t.remote_task_id LIKE ? OR t.user_email LIKE ?)))`
		q = "%" + q + "%"
		args = append(args, q, q, q, q, q)
	}
	if sku = strings.TrimSpace(sku); sku != "" {
		where += ` AND sku=?`
		args = append(args, sku)
	}
	if status = strings.TrimSpace(status); status != "" {
		where += ` AND status=?`
		args = append(args, status)
	}
	var total int
	if err := s.DB.QueryRowContext(ctx, `SELECT count(*) FROM chatgpt_cdks`+where, args...).Scan(&total); err != nil {
		return nil, 0, err
	}
	queryArgs := append(append([]any{}, args...), size, (page-1)*size)
	rows, err := s.DB.QueryContext(ctx, `SELECT `+chatGPTCDKColumns+` FROM chatgpt_cdks`+where+` ORDER BY id DESC LIMIT ? OFFSET ?`, queryArgs...)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	items := []ChatGPTCDK{}
	for rows.Next() {
		item, e := scanChatGPTCDK(rows)
		if e != nil {
			return nil, 0, e
		}
		items = append(items, *item)
	}
	return items, total, rows.Err()
}

func (s *Store) ChatGPTCDKByCode(ctx context.Context, code string) (*ChatGPTCDK, error) {
	return scanChatGPTCDK(s.DB.QueryRowContext(ctx, `SELECT `+chatGPTCDKColumns+` FROM chatgpt_cdks WHERE code=? COLLATE NOCASE`, strings.TrimSpace(code)))
}

func (s *Store) ChatGPTCDKByTaskID(ctx context.Context, taskID string, apiKeyID int64) (*ChatGPTCDK, error) {
	return scanChatGPTCDK(s.DB.QueryRowContext(ctx, `SELECT `+qualifiedChatGPTCDKColumns+` FROM chatgpt_cdks c JOIN chatgpt_tasks t ON t.id=c.task_id WHERE t.remote_task_id=? AND t.api_key_id=?`, strings.TrimSpace(taskID), apiKeyID))
}

func (s *Store) ChatGPTCDKByTaskAndCode(ctx context.Context, taskID, code string) (*ChatGPTCDK, error) {
	return scanChatGPTCDK(s.DB.QueryRowContext(ctx, `SELECT `+qualifiedChatGPTCDKColumns+` FROM chatgpt_cdks c JOIN chatgpt_tasks t ON t.id=c.task_id WHERE t.remote_task_id=? AND c.code=? COLLATE NOCASE`, strings.TrimSpace(taskID), strings.TrimSpace(code)))
}

func (s *Store) ClaimChatGPTCDK(ctx context.Context, code string) (*ChatGPTCDK, error) {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, `UPDATE chatgpt_cdks SET status='available',updated_at=CURRENT_TIMESTAMP WHERE code=? COLLATE NOCASE AND status='redeeming' AND task_id IS NULL AND updated_at<=datetime('now','-2 minutes')`, strings.TrimSpace(code)); err != nil {
		return nil, err
	}
	item, err := scanChatGPTCDK(tx.QueryRowContext(ctx, `SELECT `+chatGPTCDKColumns+` FROM chatgpt_cdks WHERE code=? COLLATE NOCASE`, strings.TrimSpace(code)))
	if err != nil {
		return nil, err
	}
	if item.Status != "available" {
		return nil, errors.New("cdk is not available")
	}
	r, err := tx.ExecContext(ctx, `UPDATE chatgpt_cdks SET status='redeeming',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='available'`, item.ID)
	if err != nil {
		return nil, err
	}
	if n, _ := r.RowsAffected(); n != 1 {
		return nil, errors.New("cdk is not available")
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	item.Status = "redeeming"
	return item, nil
}

func (s *Store) ReleaseChatGPTCDK(ctx context.Context, id int64) error {
	_, err := s.DB.ExecContext(ctx, `UPDATE chatgpt_cdks SET status='available',task_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='redeeming'`, id)
	return err
}

func (s *Store) MarkChatGPTCDKUsed(ctx context.Context, id, localTaskID int64) error {
	r, err := s.DB.ExecContext(ctx, `UPDATE chatgpt_cdks SET status='used',used_at=CURRENT_TIMESTAMP,task_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='redeeming'`, localTaskID, id)
	if err != nil {
		return err
	}
	if n, _ := r.RowsAffected(); n != 1 {
		return sql.ErrNoRows
	}
	return nil
}
