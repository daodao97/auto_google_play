package dao

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"
)

type Order struct {
	ID             int64      `json:"id"`
	BatchNo        string     `json:"batchNo"`
	Buyer          string     `json:"buyer"`
	SalePriceCents int64      `json:"salePriceCents"`
	Quantity       int        `json:"quantity"`
	Plan           string     `json:"plan"`
	ProductType    string     `json:"productType"`
	CDKSKU         string     `json:"cdkSku"`
	Status         string     `json:"status"`
	Remark         string     `json:"remark"`
	CreatedBy      int64      `json:"createdBy"`
	AllocatedAt    *time.Time `json:"allocatedAt"`
	CreatedAt      time.Time  `json:"createdAt"`
	DownloadCount  int        `json:"downloadCount"`
}

const orderColumns = `o.id,o.batch_no,o.buyer,o.sale_price_cents,o.quantity,o.plan,o.product_type,o.cdk_sku,o.status,o.remark,o.created_by,o.allocated_at,o.created_at,(SELECT count(*) FROM order_download_logs dl WHERE dl.order_id=o.id)`

func scanOrder(row interface{ Scan(...any) error }) (*Order, error) {
	o := &Order{}
	err := row.Scan(&o.ID, &o.BatchNo, &o.Buyer, &o.SalePriceCents, &o.Quantity, &o.Plan, &o.ProductType, &o.CDKSKU, &o.Status, &o.Remark, &o.CreatedBy, &o.AllocatedAt, &o.CreatedAt, &o.DownloadCount)
	return o, err
}

func (s *Store) CreateOrder(ctx context.Context, o Order, adminID int64) (*Order, error) {
	o.ProductType = strings.TrimSpace(o.ProductType)
	if o.ProductType == "" {
		o.ProductType = "claude_account"
	}
	if o.ProductType != "claude_account" && o.ProductType != "chatgpt_cdk" {
		return nil, errors.New("productType must be claude_account or chatgpt_cdk")
	}
	plan := "free"
	var err error
	if o.ProductType == "claude_account" {
		plan, err = NormalizePlan(o.Plan)
		if err != nil {
			return nil, err
		}
		o.CDKSKU = ""
	} else {
		o.CDKSKU = strings.ToLower(strings.TrimSpace(o.CDKSKU))
		if !ValidChatGPTSKU(o.CDKSKU) {
			return nil, errors.New("cdkSku must be plus, pro or prolite")
		}
	}
	o.BatchNo = strings.TrimSpace(o.BatchNo)
	o.Buyer = strings.TrimSpace(o.Buyer)
	if o.BatchNo == "" || o.Buyer == "" || o.Quantity <= 0 {
		return nil, errors.New("batchNo, buyer and positive quantity are required")
	}
	if o.SalePriceCents < 0 {
		return nil, errors.New("salePriceCents cannot be negative")
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `UPDATE claude_accounts SET delivery_status='available',locked_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE delivery_status='locked' AND locked_until IS NOT NULL AND locked_until<=unixepoch()`); err != nil {
		return nil, err
	}
	r, err := tx.ExecContext(ctx, `INSERT INTO orders(batch_no,buyer,sale_price_cents,quantity,plan,product_type,cdk_sku,remark,created_by) VALUES(?,?,?,?,?,?,?,?,?)`, o.BatchNo, o.Buyer, o.SalePriceCents, o.Quantity, plan, o.ProductType, o.CDKSKU, strings.TrimSpace(o.Remark), adminID)
	if err != nil {
		return nil, err
	}
	orderID, err := r.LastInsertId()
	if err != nil {
		return nil, err
	}
	selection := `SELECT id FROM claude_accounts a WHERE plan=? AND status=1 AND alive_status<>'dead' AND ((?='max_20x' AND delivery_status IN ('available','upgraded')) OR (?='free' AND delivery_status='available')) AND NOT EXISTS(SELECT 1 FROM order_accounts oa WHERE oa.account_id=a.id) ORDER BY id ASC LIMIT ?`
	selectionArgs := []any{plan, plan, plan, o.Quantity}
	if o.ProductType == "chatgpt_cdk" {
		selection = `SELECT id FROM chatgpt_cdks c WHERE sku=? AND status='available' AND NOT EXISTS(SELECT 1 FROM order_cdks oc WHERE oc.cdk_id=c.id) ORDER BY id ASC LIMIT ?`
		selectionArgs = []any{o.CDKSKU, o.Quantity}
	}
	rows, err := tx.QueryContext(ctx, selection, selectionArgs...)
	if err != nil {
		return nil, err
	}
	ids := []int64{}
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return nil, err
		}
		ids = append(ids, id)
	}
	rows.Close()
	if len(ids) != o.Quantity {
		if o.ProductType == "chatgpt_cdk" {
			return nil, fmt.Errorf("insufficient chatgpt cdks: available=%d requested=%d", len(ids), o.Quantity)
		}
		return nil, fmt.Errorf("insufficient accounts: available=%d requested=%d", len(ids), o.Quantity)
	}
	for _, id := range ids {
		if o.ProductType == "chatgpt_cdk" {
			if _, err := tx.ExecContext(ctx, `INSERT INTO order_cdks(order_id,cdk_id) VALUES(?,?)`, orderID, id); err != nil {
				return nil, err
			}
			continue
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO order_accounts(order_id,account_id) VALUES(?,?)`, orderID, id); err != nil {
			return nil, err
		}
		result, err := tx.ExecContext(ctx, `UPDATE claude_accounts SET delivery_status='sold',locked_until=NULL,delivered_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status=1 AND alive_status<>'dead' AND delivery_status IN ('available','upgraded')`, id)
		if err != nil {
			return nil, err
		}
		if affected, _ := result.RowsAffected(); affected != 1 {
			return nil, errors.New("account is no longer available for sale")
		}
	}
	now := time.Now()
	if _, err := tx.ExecContext(ctx, `UPDATE orders SET status='allocated',allocated_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?`, now, orderID); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return s.OrderByID(ctx, orderID)
}
func (s *Store) OrderByID(ctx context.Context, id int64) (*Order, error) {
	return scanOrder(s.DB.QueryRowContext(ctx, `SELECT `+orderColumns+` FROM orders o WHERE o.id=?`, id))
}
func (s *Store) ListOrders(ctx context.Context, page, size int, query, productType, plan, cdkSKU, status string) ([]Order, int, error) {
	where := ` WHERE 1=1`
	args := []any{}
	if productType == "chatgpt_cdk" {
		plan = ""
	}
	if productType == "claude_account" {
		cdkSKU = ""
	}
	if query != "" {
		where += ` AND (o.batch_no LIKE ? OR o.buyer LIKE ?)`
		q := "%" + strings.TrimSpace(query) + "%"
		args = append(args, q, q)
	}
	if plan != "" {
		where += ` AND o.plan=?`
		args = append(args, plan)
	}
	if productType != "" {
		where += ` AND o.product_type=?`
		args = append(args, productType)
	}
	if cdkSKU != "" {
		where += ` AND o.cdk_sku=?`
		args = append(args, cdkSKU)
	}
	if status != "" {
		where += ` AND o.status=?`
		args = append(args, status)
	}
	var total int
	if err := s.DB.QueryRowContext(ctx, `SELECT count(*) FROM orders o`+where, args...).Scan(&total); err != nil {
		return nil, 0, err
	}
	args = append(args, size, (page-1)*size)
	rows, err := s.DB.QueryContext(ctx, `SELECT `+orderColumns+` FROM orders o`+where+` ORDER BY o.id DESC LIMIT ? OFFSET ?`, args...)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	items := []Order{}
	for rows.Next() {
		o, e := scanOrder(rows)
		if e != nil {
			return nil, 0, e
		}
		items = append(items, *o)
	}
	return items, total, rows.Err()
}
func (s *Store) OrderAccounts(ctx context.Context, id int64) ([]ClaudeAccount, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT `+qualifiedAccountColumns("a")+` FROM order_accounts oa JOIN claude_accounts a ON a.id=oa.account_id WHERE oa.order_id=? ORDER BY oa.id`, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []ClaudeAccount{}
	for rows.Next() {
		a, e := scanAccount(rows)
		if e != nil {
			return nil, e
		}
		items = append(items, *a)
	}
	return items, rows.Err()
}
func (s *Store) OrderCDKs(ctx context.Context, id int64) ([]ChatGPTCDK, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT `+qualifiedChatGPTCDKColumns+` FROM order_cdks oc JOIN chatgpt_cdks c ON c.id=oc.cdk_id WHERE oc.order_id=? ORDER BY oc.id`, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []ChatGPTCDK{}
	for rows.Next() {
		item, e := scanChatGPTCDK(rows)
		if e != nil {
			return nil, e
		}
		items = append(items, *item)
	}
	return items, rows.Err()
}
func (s *Store) RecordOrderDownload(ctx context.Context, orderID, adminID int64, ip string) error {
	_, err := s.DB.ExecContext(ctx, `INSERT INTO order_download_logs(order_id,admin_user_id,client_ip) VALUES(?,?,?)`, orderID, adminID, ip)
	return err
}
func (s *Store) CancelOrder(ctx context.Context, id int64) error {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var status string
	var downloads int
	err = tx.QueryRowContext(ctx, `SELECT status,(SELECT count(*) FROM order_download_logs WHERE order_id=orders.id) FROM orders WHERE id=?`, id).Scan(&status, &downloads)
	if err != nil {
		return err
	}
	if status == "cancelled" {
		return tx.Commit()
	}
	if downloads > 0 {
		return errors.New("downloaded order cannot be cancelled")
	}
	var productType string
	if err = tx.QueryRowContext(ctx, `SELECT product_type FROM orders WHERE id=?`, id).Scan(&productType); err != nil {
		return err
	}
	if productType == "chatgpt_cdk" {
		if _, err = tx.ExecContext(ctx, `DELETE FROM order_cdks WHERE order_id=?`, id); err != nil {
			return err
		}
		if _, err = tx.ExecContext(ctx, `UPDATE orders SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=?`, id); err != nil {
			return err
		}
		return tx.Commit()
	}
	if _, err = tx.ExecContext(ctx, `UPDATE claude_accounts SET delivery_status=CASE WHEN plan='max_20x' THEN 'upgraded' ELSE 'available' END,delivered_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id IN (SELECT account_id FROM order_accounts WHERE order_id=?)`, id); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, `DELETE FROM order_accounts WHERE order_id=?`, id); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, `UPDATE orders SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=?`, id); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Store) ListAPIKeys(ctx context.Context) ([]APIKey, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT id,name,key_prefix,status,last_used_at,expires_at,created_at FROM api_keys WHERE deleted_at IS NULL ORDER BY id DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []APIKey{}
	for rows.Next() {
		var k APIKey
		if err := rows.Scan(&k.ID, &k.Name, &k.Prefix, &k.Status, &k.LastUsedAt, &k.ExpiresAt, &k.CreatedAt); err != nil {
			return nil, err
		}
		items = append(items, k)
	}
	return items, rows.Err()
}
func (s *Store) SetAPIKeyStatus(ctx context.Context, id int64, status int) error {
	if status != 1 && status != -1 {
		return errors.New("status must be 1 or -1")
	}
	r, err := s.DB.ExecContext(ctx, `UPDATE api_keys SET status=? WHERE id=? AND deleted_at IS NULL`, status, id)
	if err != nil {
		return err
	}
	n, _ := r.RowsAffected()
	if n == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *Store) DeleteAPIKey(ctx context.Context, id int64) error {
	r, err := s.DB.ExecContext(ctx, `UPDATE api_keys SET status=-1,deleted_at=CURRENT_TIMESTAMP WHERE id=? AND deleted_at IS NULL`, id)
	if err != nil {
		return err
	}
	if n, _ := r.RowsAffected(); n == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *Store) ListAdmins(ctx context.Context) ([]Admin, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT `+adminColumns+` FROM admin_users ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []Admin{}
	for rows.Next() {
		a, e := scanAdmin(rows)
		if e != nil {
			return nil, e
		}
		items = append(items, *a)
	}
	return items, rows.Err()
}
func (s *Store) UpdateAdmin(ctx context.Context, id int64, name, role string, status int, passwordHash string) error {
	if role != "admin" && role != "super_admin" {
		return errors.New("invalid role")
	}
	if status != 1 && status != -1 {
		return errors.New("invalid status")
	}
	var r sql.Result
	var err error
	if passwordHash != "" {
		r, err = s.DB.ExecContext(ctx, `UPDATE admin_users SET display_name=?,role=?,status=?,password_hash=?,updated_at=CURRENT_TIMESTAMP WHERE id=?`, strings.TrimSpace(name), role, status, passwordHash, id)
	} else {
		r, err = s.DB.ExecContext(ctx, `UPDATE admin_users SET display_name=?,role=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?`, strings.TrimSpace(name), role, status, id)
	}
	if err != nil {
		return err
	}
	n, _ := r.RowsAffected()
	if n == 0 {
		return ErrNotFound
	}
	return nil
}
func (s *Store) ActiveSuperAdminCount(ctx context.Context) (int, error) {
	var n int
	err := s.DB.QueryRowContext(ctx, `SELECT count(*) FROM admin_users WHERE role='super_admin' AND status=1`).Scan(&n)
	return n, err
}

func (s *Store) Audit(ctx context.Context, actorType string, actorID int64, action, resourceType, resourceID, detail, ip string) {
	if detail == "" {
		detail = "{}"
	}
	_, _ = s.DB.ExecContext(ctx, `INSERT INTO audit_logs(actor_type,actor_id,action,resource_type,resource_id,detail_json,ip) VALUES(?,?,?,?,?,?,?)`, actorType, actorID, action, resourceType, resourceID, detail, ip)
}
