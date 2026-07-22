package dao

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/base64"
	"strings"
	"time"
)

type ChatGPTTask struct {
	ID           int64      `json:"id"`
	HashID       string     `json:"hashId"`
	CDKID        int64      `json:"cdkId"`
	CDKCode      string     `json:"cdk"`
	UserEmail    string     `json:"userEmail"`
	Session      string     `json:"-"`
	SKU          string     `json:"sku"`
	Channel      string     `json:"channel"`
	APIKeyID     *int64     `json:"apiKeyId,omitempty"`
	RemoteTaskID string     `json:"remoteTaskId"`
	Status       string     `json:"status"`
	ResultJSON   string     `json:"resultJson,omitempty"`
	ErrorCode    string     `json:"errorCode,omitempty"`
	ErrorMessage string     `json:"errorMessage,omitempty"`
	ClientIP     string     `json:"clientIp"`
	CreatedAt    time.Time  `json:"createdAt"`
	UpdatedAt    time.Time  `json:"updatedAt"`
	FinishedAt   *time.Time `json:"finishedAt,omitempty"`
}

func (s *Store) ListChatGPTTasks(ctx context.Context, page, size int, query, status string) ([]ChatGPTTask, int, error) {
	where, args := ` WHERE 1=1`, []any{}
	if q := strings.TrimSpace(query); q != "" {
		where += ` AND (hash_id LIKE ? OR cdk_code LIKE ? OR user_email LIKE ? OR remote_task_id LIKE ?)`
		q = "%" + q + "%"
		args = append(args, q, q, q, q)
	}
	if status = strings.TrimSpace(status); status != "" {
		where += ` AND status=?`
		args = append(args, status)
	}
	var total int
	if err := s.DB.QueryRowContext(ctx, `SELECT count(*) FROM chatgpt_tasks`+where, args...).Scan(&total); err != nil {
		return nil, 0, err
	}
	queryArgs := append(append([]any{}, args...), size, (page-1)*size)
	rows, err := s.DB.QueryContext(ctx, `SELECT `+chatGPTTaskColumns+` FROM chatgpt_tasks`+where+` ORDER BY id DESC LIMIT ? OFFSET ?`, queryArgs...)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	items := []ChatGPTTask{}
	for rows.Next() {
		item, scanErr := scanChatGPTTask(rows)
		if scanErr != nil {
			return nil, 0, scanErr
		}
		items = append(items, *item)
	}
	return items, total, rows.Err()
}

const chatGPTTaskColumns = `id,hash_id,cdk_id,cdk_code,user_email,session,sku,channel,api_key_id,remote_task_id,status,result_json,error_code,error_message,client_ip,created_at,updated_at,finished_at`

func scanChatGPTTask(row interface{ Scan(...any) error }) (*ChatGPTTask, error) {
	t := &ChatGPTTask{}
	err := row.Scan(&t.ID, &t.HashID, &t.CDKID, &t.CDKCode, &t.UserEmail, &t.Session, &t.SKU, &t.Channel, &t.APIKeyID, &t.RemoteTaskID, &t.Status, &t.ResultJSON, &t.ErrorCode, &t.ErrorMessage, &t.ClientIP, &t.CreatedAt, &t.UpdatedAt, &t.FinishedAt)
	return t, err
}

func (s *Store) CreateChatGPTTask(ctx context.Context, cdk ChatGPTCDK, userEmail, session, channel, clientIP string, apiKeyID int64) (*ChatGPTTask, error) {
	var owner any
	if apiKeyID > 0 {
		owner = apiKeyID
	}
	random := make([]byte, 20)
	if _, err := rand.Read(random); err != nil {
		return nil, err
	}
	hashID := "ctk_" + base64.RawURLEncoding.EncodeToString(random)
	r, err := s.DB.ExecContext(ctx, `INSERT INTO chatgpt_tasks(hash_id,cdk_id,cdk_code,user_email,session,sku,channel,api_key_id,client_ip) VALUES(?,?,?,?,?,?,?,?,?)`, hashID, cdk.ID, cdk.Code, userEmail, session, cdk.SKU, channel, owner, clientIP)
	if err != nil {
		return nil, err
	}
	id, err := r.LastInsertId()
	if err != nil {
		return nil, err
	}
	return s.ChatGPTTaskByID(ctx, id)
}

func (s *Store) ChatGPTTaskByHashAndCode(ctx context.Context, hashID, code string) (*ChatGPTTask, error) {
	return scanChatGPTTask(s.DB.QueryRowContext(ctx, `SELECT `+chatGPTTaskColumns+` FROM chatgpt_tasks WHERE hash_id=? AND cdk_code=? COLLATE NOCASE`, strings.TrimSpace(hashID), strings.TrimSpace(code)))
}

func (s *Store) ChatGPTTaskByHashAndAPIKey(ctx context.Context, hashID string, apiKeyID int64) (*ChatGPTTask, error) {
	return scanChatGPTTask(s.DB.QueryRowContext(ctx, `SELECT `+chatGPTTaskColumns+` FROM chatgpt_tasks WHERE hash_id=? AND api_key_id=?`, strings.TrimSpace(hashID), apiKeyID))
}

func (s *Store) ChatGPTTaskByID(ctx context.Context, id int64) (*ChatGPTTask, error) {
	return scanChatGPTTask(s.DB.QueryRowContext(ctx, `SELECT `+chatGPTTaskColumns+` FROM chatgpt_tasks WHERE id=?`, id))
}

func (s *Store) SetChatGPTTaskRemote(ctx context.Context, id int64, remoteTaskID, status, resultJSON string) error {
	r, err := s.DB.ExecContext(ctx, `UPDATE chatgpt_tasks SET remote_task_id=?,status=?,result_json=?,error_code='',error_message='',updated_at=CURRENT_TIMESTAMP WHERE id=?`, remoteTaskID, status, resultJSON, id)
	if err != nil {
		return err
	}
	if n, _ := r.RowsAffected(); n != 1 {
		return sql.ErrNoRows
	}
	return nil
}

func (s *Store) FinalizeChatGPTTaskCreated(ctx context.Context, cdkID, localTaskID int64, remoteTaskID, status, resultJSON string) error {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	result, err := tx.ExecContext(ctx, `UPDATE chatgpt_tasks SET remote_task_id=?,status=?,result_json=?,error_code='',error_message='',updated_at=CURRENT_TIMESTAMP WHERE id=?`, remoteTaskID, status, resultJSON, localTaskID)
	if err != nil {
		return err
	}
	if n, _ := result.RowsAffected(); n != 1 {
		return sql.ErrNoRows
	}
	result, err = tx.ExecContext(ctx, `UPDATE chatgpt_cdks SET status='used',used_at=CURRENT_TIMESTAMP,task_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='redeeming'`, localTaskID, cdkID)
	if err != nil {
		return err
	}
	if n, _ := result.RowsAffected(); n != 1 {
		return sql.ErrNoRows
	}
	return tx.Commit()
}

func (s *Store) FailChatGPTTaskCreation(ctx context.Context, id int64, errorCode, errorMessage, resultJSON string) error {
	r, err := s.DB.ExecContext(ctx, `UPDATE chatgpt_tasks SET status='create_failed',result_json=?,error_code=?,error_message=?,finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?`, resultJSON, errorCode, errorMessage, id)
	if err != nil {
		return err
	}
	if n, _ := r.RowsAffected(); n != 1 {
		return sql.ErrNoRows
	}
	return nil
}

func (s *Store) UpdateChatGPTTask(ctx context.Context, remoteTaskID, status, errorCode, errorMessage, resultJSON string) error {
	finished := `NULL`
	if status == "success" || status == "failed" {
		finished = `CURRENT_TIMESTAMP`
	}
	r, err := s.DB.ExecContext(ctx, `UPDATE chatgpt_tasks SET status=?,result_json=?,error_code=?,error_message=?,finished_at=`+finished+`,updated_at=CURRENT_TIMESTAMP WHERE remote_task_id=?`, status, resultJSON, errorCode, errorMessage, remoteTaskID)
	if err != nil {
		return err
	}
	if n, _ := r.RowsAffected(); n != 1 {
		return sql.ErrNoRows
	}
	return nil
}
