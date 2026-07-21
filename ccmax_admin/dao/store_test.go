package dao

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func testStore(t *testing.T) *Store {
	t.Helper()
	store, err := Open(filepath.Join(t.TempDir(), "ccmax.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })
	return store
}
func seedAdminAndKey(t *testing.T, s *Store) (int64, int64) {
	t.Helper()
	ctx := context.Background()
	adminID, err := s.CreateAdmin(ctx, "root", "hash", "Root", "super_admin")
	if err != nil {
		t.Fatal(err)
	}
	keyID, err := s.CreateAPIKey(ctx, "test", "ccm_test", "hash", adminID)
	if err != nil {
		t.Fatal(err)
	}
	return adminID, keyID
}
func seedAccounts(t *testing.T, s *Store, plan string, count int) {
	t.Helper()
	for i := 0; i < count; i++ {
		_, err := s.CreateAccount(context.Background(), ClaudeAccount{Mail: fmt.Sprintf("user-%s-%d@example.com", plan, i), Password: "pass", SessionKey: fmt.Sprintf("session-%s-%d", plan, i), Plan: plan})
		if err != nil {
			t.Fatal(err)
		}
	}
}

func TestDispatchLeaseAndIdempotency(t *testing.T) {
	s := testStore(t)
	_, keyID := seedAdminAndKey(t, s)
	seedAccounts(t, s, "free", 3)
	ctx := context.Background()
	first, err := s.DispatchAccounts(ctx, keyID, "req-1", 2, time.Hour, "127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	retry, err := s.DispatchAccounts(ctx, keyID, "req-1", 2, time.Hour, "127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	if len(retry) != 2 || retry[0].ID != first[0].ID || retry[1].ID != first[1].ID {
		t.Fatalf("idempotent retry changed accounts: %#v %#v", first, retry)
	}
	if first[0].DeliveryStatus != "locked" || first[0].LockedUntil == nil || first[0].LockRequestID != "req-1" {
		t.Fatalf("dispatched account was not leased: %#v", first[0])
	}
	second, err := s.DispatchAccounts(ctx, keyID, "req-2", 1, time.Hour, "127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	if second[0].ID == first[0].ID || second[0].ID == first[1].ID {
		t.Fatal("leased account was dispatched again")
	}
	if _, err = s.DispatchAccounts(ctx, keyID, "req-3", 1, time.Hour, "127.0.0.1"); err == nil {
		t.Fatal("expected insufficient inventory")
	}
}

func TestDeadClaudeAccountsAreNotDispatchedOrSold(t *testing.T) {
	s := testStore(t)
	adminID, keyID := seedAdminAndKey(t, s)
	ctx := t.Context()
	deadFreeID, err := s.CreateAccount(ctx, ClaudeAccount{Mail: "dead-free@example.com", Password: "pass", SessionKey: "dead-free-session", Plan: "free"})
	if err != nil {
		t.Fatal(err)
	}
	aliveFreeID, err := s.CreateAccount(ctx, ClaudeAccount{Mail: "alive-free@example.com", Password: "pass", SessionKey: "alive-free-session", Plan: "free"})
	if err != nil {
		t.Fatal(err)
	}
	if err = s.SetAccountAliveStatus(ctx, deadFreeID, "dead", time.Now()); err != nil {
		t.Fatal(err)
	}
	if err = s.SetAccountAliveStatus(ctx, aliveFreeID, "alive", time.Now()); err != nil {
		t.Fatal(err)
	}
	dashboard, err := s.Dashboard(ctx)
	if err != nil || dashboard["freeAccounts"] != int64(1) {
		t.Fatalf("dead Free account was counted as inventory: %#v %v", dashboard, err)
	}
	dispatched, err := s.DispatchAccounts(ctx, keyID, "alive-only", 1, time.Hour, "127.0.0.1")
	if err != nil || len(dispatched) != 1 || dispatched[0].ID != aliveFreeID {
		t.Fatalf("dead Free account was dispatched: %#v %v", dispatched, err)
	}
	if err = s.SetAccountAliveStatus(ctx, aliveFreeID, "dead", time.Now()); err != nil {
		t.Fatal(err)
	}
	if _, err = s.DispatchAccounts(ctx, keyID, "alive-only", 1, time.Hour, "127.0.0.1"); err == nil {
		t.Fatal("idempotent retry returned an account that became dead")
	}

	deadMaxID, err := s.CreateAccount(ctx, ClaudeAccount{Mail: "dead-max@example.com", Password: "pass", SessionKey: "dead-max-session", Plan: "max_20x"})
	if err != nil {
		t.Fatal(err)
	}
	aliveMaxID, err := s.CreateAccount(ctx, ClaudeAccount{Mail: "alive-max@example.com", Password: "pass", SessionKey: "alive-max-session", Plan: "max_20x"})
	if err != nil {
		t.Fatal(err)
	}
	if err = s.SetAccountAliveStatus(ctx, deadMaxID, "dead", time.Now()); err != nil {
		t.Fatal(err)
	}
	if err = s.SetAccountAliveStatus(ctx, aliveMaxID, "alive", time.Now()); err != nil {
		t.Fatal(err)
	}
	dashboard, err = s.Dashboard(ctx)
	if err != nil || dashboard["maxAccounts"] != int64(1) {
		t.Fatalf("dead Max account was counted as inventory: %#v %v", dashboard, err)
	}
	order, err := s.CreateOrder(ctx, Order{BatchNo: "alive-max-only", Buyer: "buyer", Quantity: 1, Plan: "max_20x"}, adminID)
	if err != nil {
		t.Fatal(err)
	}
	accounts, err := s.OrderAccounts(ctx, order.ID)
	if err != nil || len(accounts) != 1 || accounts[0].ID != aliveMaxID {
		t.Fatalf("dead Max account was allocated to an order: %#v %v", accounts, err)
	}
}

func TestUpgradeReleaseAndLeaseExpiry(t *testing.T) {
	s := testStore(t)
	_, keyID := seedAdminAndKey(t, s)
	seedAccounts(t, s, "free", 2)
	ctx := context.Background()

	first, err := s.DispatchAccounts(ctx, keyID, "success-request", 1, time.Hour, "127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	upgraded, err := s.UpgradeAccount(ctx, first[0].Mail, "max_20x", time.Now(), 101)
	if err != nil || upgraded.DeliveryStatus != "upgraded" || upgraded.Plan != "max_20x" || upgraded.CardPoolID == nil || *upgraded.CardPoolID != 101 || upgraded.DeliveredAt != nil || upgraded.LockedUntil != nil {
		t.Fatalf("upgrade sync did not finalize upgrade: %#v %v", upgraded, err)
	}

	second, err := s.DispatchAccounts(ctx, keyID, "failed-request", 1, time.Hour, "127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	released, err := s.ReleaseDispatchedAccount(ctx, keyID, "failed-request", second[0].Mail)
	if err != nil || released.DeliveryStatus != "available" {
		t.Fatalf("failed account was not released: %#v %v", released, err)
	}
	reused, err := s.DispatchAccounts(ctx, keyID, "reuse-request", 1, time.Hour, "127.0.0.1")
	if err != nil || reused[0].ID != second[0].ID {
		t.Fatalf("released account was not reused first: %#v %v", reused, err)
	}

	if _, err = s.DB.ExecContext(ctx, `UPDATE claude_accounts SET locked_until=unixepoch()-1 WHERE id=?`, reused[0].ID); err != nil {
		t.Fatal(err)
	}
	expired, err := s.DispatchAccounts(ctx, keyID, "expired-request", 1, time.Hour, "127.0.0.1")
	if err != nil || expired[0].ID != reused[0].ID {
		t.Fatalf("expired lease was not automatically recovered: %#v %v", expired, err)
	}
	if _, err = s.ReleaseDispatchedAccount(ctx, keyID, "reuse-request", reused[0].Mail); err == nil {
		t.Fatal("stale request released an account after reassignment")
	}
	if _, err = s.DispatchAccounts(ctx, keyID, "reuse-request", 1, time.Hour, "127.0.0.1"); err == nil {
		t.Fatal("expired idempotency key returned an account after reassignment")
	}
}

func TestUpgradeAccountCoolsCardForFiveHours(t *testing.T) {
	s := testStore(t)
	_, keyID := seedAdminAndKey(t, s)
	seedAccounts(t, s, "free", 1)
	ctx := t.Context()
	cardIDs := make([]int64, 2)
	for i := range cardIDs {
		id, err := s.CreateCard(ctx, Card{Source: "qbit", CardID: fmt.Sprintf("cooldown-%d", i), CardNo: fmt.Sprintf("422222222222222%d", i), ExpireMMYY: "1228", CCV: fmt.Sprintf("45%d", i)})
		if err != nil {
			t.Fatal(err)
		}
		cardIDs[i] = id
	}
	accounts, _, err := s.ListAccounts(ctx, 1, 1, "", "free", 1)
	if err != nil || len(accounts) != 1 {
		t.Fatalf("list account: %#v %v", accounts, err)
	}
	before := time.Now()
	if _, err = s.UpgradeAccount(ctx, accounts[0].Mail, "max_20x", before, cardIDs[0]); err != nil {
		t.Fatal(err)
	}
	cooled, err := s.CardByID(ctx, cardIDs[0])
	if err != nil || cooled.UsageCount != 1 || cooled.CooldownUntil == nil || cooled.CooldownUntil.Before(before.Add(4*time.Hour+59*time.Minute)) || cooled.CooldownUntil.After(time.Now().Add(5*time.Hour+time.Minute)) {
		t.Fatalf("card cooldown was not set to five hours: %#v %v", cooled, err)
	}
	if _, err = s.UpgradeAccount(ctx, accounts[0].Mail, "max_20x", time.Now(), cardIDs[0]); err != nil {
		t.Fatal(err)
	}
	cooled, err = s.CardByID(ctx, cardIDs[0])
	if err != nil || cooled.UsageCount != 1 {
		t.Fatalf("idempotent upgrade report incremented usage twice: %#v %v", cooled, err)
	}
	stats, err := s.CardStats(ctx)
	if err != nil || stats.Available != 1 || stats.Cooling != 1 || stats.Total != 2 {
		t.Fatalf("unexpected card stats during cooldown: %#v %v", stats, err)
	}
	dispatched, err := s.DispatchCards(ctx, keyID, "during-cooldown", "qbit", 1, "127.0.0.1")
	if err != nil || len(dispatched) != 1 || dispatched[0].ID != cardIDs[1] {
		t.Fatalf("cooled card was dispatched: %#v %v", dispatched, err)
	}
	if _, err = s.DB.ExecContext(ctx, `UPDATE card_pool SET cooldown_until=? WHERE id=?`, time.Now().Add(-time.Minute), cardIDs[0]); err != nil {
		t.Fatal(err)
	}
	stats, err = s.CardStats(ctx)
	if err != nil || stats.Available != 2 || stats.Cooling != 0 || stats.Total != 2 {
		t.Fatalf("unexpected card stats after cooldown: %#v %v", stats, err)
	}
	dispatched, err = s.DispatchCards(ctx, keyID, "after-cooldown", "qbit", 2, "127.0.0.1")
	if err != nil || len(dispatched) != 2 || dispatched[0].ID != cardIDs[1] || dispatched[1].ID != cardIDs[0] {
		t.Fatalf("expired cooldown card was not reusable: %#v %v", dispatched, err)
	}
}

func TestMigrateUpgradeStatusKeepsOrderDeliveries(t *testing.T) {
	s := testStore(t)
	adminID, _ := seedAdminAndKey(t, s)
	seedAccounts(t, s, "max_20x", 2)
	ctx := context.Background()
	order, err := s.CreateOrder(ctx, Order{BatchNo: "upgrade-migration", Buyer: "buyer", Quantity: 1, Plan: "max_20x"}, adminID)
	if err != nil {
		t.Fatal(err)
	}
	orderAccounts, err := s.OrderAccounts(ctx, order.ID)
	if err != nil || len(orderAccounts) != 1 {
		t.Fatalf("order accounts: %#v %v", orderAccounts, err)
	}
	if _, err = s.DB.ExecContext(ctx, `UPDATE claude_accounts SET upgraded_at=CURRENT_TIMESTAMP,delivery_status='delivered',delivered_at=CURRENT_TIMESTAMP`); err != nil {
		t.Fatal(err)
	}
	if err = s.migrateAccountLease(ctx); err != nil {
		t.Fatal(err)
	}
	var orderStatus string
	if err = s.DB.QueryRowContext(ctx, `SELECT delivery_status FROM claude_accounts WHERE id=?`, orderAccounts[0].ID).Scan(&orderStatus); err != nil || orderStatus != "sold" {
		t.Fatalf("order delivery status = %q, err=%v", orderStatus, err)
	}
	var upgradedStatus string
	if err = s.DB.QueryRowContext(ctx, `SELECT delivery_status FROM claude_accounts WHERE id<>?`, orderAccounts[0].ID).Scan(&upgradedStatus); err != nil || upgradedStatus != "upgraded" {
		t.Fatalf("non-order upgrade status = %q, err=%v", upgradedStatus, err)
	}
}

func TestConcurrentDispatchNeverReturnsSameAccount(t *testing.T) {
	s := testStore(t)
	_, keyID := seedAdminAndKey(t, s)
	seedAccounts(t, s, "free", 2)
	var wg sync.WaitGroup
	results := make(chan int64, 2)
	errs := make(chan error, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			items, err := s.DispatchAccounts(context.Background(), keyID, fmt.Sprintf("concurrent-%d", i), 1, time.Hour, "127.0.0.1")
			if err != nil {
				errs <- err
				return
			}
			results <- items[0].ID
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}
	close(results)
	ids := map[int64]bool{}
	for id := range results {
		if ids[id] {
			t.Fatalf("account %d was dispatched twice", id)
		}
		ids[id] = true
	}
	if len(ids) != 2 {
		t.Fatalf("expected two distinct accounts, got %v", ids)
	}
}

func TestCardDispatchReportIdempotencyAndOwnership(t *testing.T) {
	s := testStore(t)
	adminID, keyID := seedAdminAndKey(t, s)
	otherKeyID, err := s.CreateAPIKey(t.Context(), "other", "ccm_other", "other-hash", adminID)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 3; i++ {
		_, err = s.CreateCard(t.Context(), Card{Source: "qbit", CardID: fmt.Sprintf("channel-%d", i), CardNo: fmt.Sprintf("411111111111111%d", i), ExpireMMYY: "1228", CCV: fmt.Sprintf("12%d", i)})
		if err != nil {
			t.Fatal(err)
		}
	}
	first, err := s.DispatchCards(t.Context(), keyID, "card-request-1", "qbit", 2, "127.0.0.1")
	if err != nil || len(first) != 2 || first[0].UsageCount != 0 || first[0].LastDispatchedAt == nil {
		t.Fatalf("unexpected card dispatch: %#v %v", first, err)
	}
	retry, err := s.DispatchCards(t.Context(), keyID, "card-request-1", "qbit", 2, "127.0.0.1")
	if err != nil || retry[0].ID != first[0].ID || retry[1].ID != first[1].ID {
		t.Fatalf("card idempotency changed result: %#v %v", retry, err)
	}
	third, err := s.DispatchCards(t.Context(), keyID, "card-request-2", "qbit", 1, "127.0.0.1")
	if err != nil || third[0].ID == first[0].ID || third[0].ID == first[1].ID {
		t.Fatalf("least-used card was not selected: %#v %v", third, err)
	}
	if _, err = s.CardByIDForAPIKey(t.Context(), first[0].ID, keyID); err != nil {
		t.Fatalf("dispatching API key cannot access its card: %v", err)
	}
	if _, err = s.CardByIDForAPIKey(t.Context(), first[0].ID, otherKeyID); !IsNoRows(err) {
		t.Fatalf("other API key accessed card: %v", err)
	}
	reassigned, err := s.DispatchCards(t.Context(), otherKeyID, "other-card-request", "qbit", 1, "127.0.0.1")
	if err != nil || reassigned[0].ID != first[0].ID {
		t.Fatalf("card was not immediately reusable: %#v %v", reassigned, err)
	}
	if _, err = s.CardByIDForAPIKey(t.Context(), first[0].ID, keyID); err != nil {
		t.Fatalf("previous dispatcher lost verification access to reusable card: %v", err)
	}
	oldRetry, err := s.DispatchCards(t.Context(), keyID, "card-request-1", "qbit", 2, "127.0.0.1")
	if err != nil || oldRetry[0].ID != first[0].ID {
		t.Fatalf("card idempotency retry did not return original result: %#v %v", oldRetry, err)
	}
	reported, err := s.ReportCardUnavailable(t.Context(), otherKeyID, "other-card-request", reassigned[0].ID)
	if err != nil || reported.Status != -1 {
		t.Fatalf("card unavailable report was not applied: %#v %v", reported, err)
	}
	if _, err = s.ReportCardUnavailable(t.Context(), otherKeyID, "other-card-request", reassigned[0].ID); err != nil {
		t.Fatalf("card unavailable report should be idempotent: %v", err)
	}
}

func TestOrderAllocationIsUniqueAndAtomic(t *testing.T) {
	s := testStore(t)
	adminID, _ := seedAdminAndKey(t, s)
	seedAccounts(t, s, "max_20x", 2)
	ctx := context.Background()
	order, err := s.CreateOrder(ctx, Order{BatchNo: "batch-1", Buyer: "buyer", SalePriceCents: 19900, Quantity: 2, Plan: "max_20x"}, adminID)
	if err != nil {
		t.Fatal(err)
	}
	accounts, err := s.OrderAccounts(ctx, order.ID)
	if err != nil || len(accounts) != 2 {
		t.Fatalf("unexpected allocated accounts: %d %v", len(accounts), err)
	}
	for _, account := range accounts {
		if account.DeliveryStatus != "sold" {
			t.Fatalf("order account not marked sold: %#v", account)
		}
	}
	listed, _, err := s.ListAccounts(ctx, 1, 20, "", "", 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, account := range listed {
		if account.DeliveryStatus == "sold" && account.OrderBatchNo != "batch-1" {
			t.Fatalf("sold account missing order batch number: %#v", account)
		}
	}
	if _, err = s.CreateOrder(ctx, Order{BatchNo: "batch-2", Buyer: "buyer", Quantity: 1, Plan: "max_20x"}, adminID); err == nil {
		t.Fatal("expected insufficient inventory")
	}
	var count int
	if err = s.DB.QueryRow(`SELECT count(*) FROM orders WHERE batch_no='batch-2'`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatal("failed allocation did not roll back order")
	}
}

func TestUpgradedMaxAccountIsAvailableForOrders(t *testing.T) {
	s := testStore(t)
	adminID, keyID := seedAdminAndKey(t, s)
	seedAccounts(t, s, "free", 1)
	ctx := t.Context()
	dispatched, err := s.DispatchAccounts(ctx, keyID, "upgrade-for-order", 1, time.Hour, "127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.UpgradeAccount(ctx, dispatched[0].Mail, "max_20x", time.Now(), 999); err != nil {
		t.Fatal(err)
	}
	dashboard, err := s.Dashboard(ctx)
	if err != nil || dashboard["maxAccounts"] != int64(1) {
		t.Fatalf("upgraded account missing from dashboard inventory: %#v %v", dashboard, err)
	}
	order, err := s.CreateOrder(ctx, Order{BatchNo: "upgraded-max", Buyer: "buyer", Quantity: 1, Plan: "max_20x"}, adminID)
	if err != nil {
		t.Fatalf("create order from upgraded inventory: %v", err)
	}
	accounts, err := s.OrderAccounts(ctx, order.ID)
	if err != nil || len(accounts) != 1 || accounts[0].DeliveryStatus != "sold" {
		t.Fatalf("upgraded account was not sold: %#v %v", accounts, err)
	}
	if err = s.CancelOrder(ctx, order.ID); err != nil {
		t.Fatal(err)
	}
	account, err := scanAccount(s.DB.QueryRowContext(ctx, `SELECT `+accountColumns+` FROM claude_accounts WHERE id=?`, accounts[0].ID))
	if err != nil || account.DeliveryStatus != "upgraded" {
		t.Fatalf("cancelled Max account did not return to upgraded inventory: %#v %v", account, err)
	}
}

func TestDeleteAccountCleansDispatchesAndPreservesOrderAccounts(t *testing.T) {
	s := testStore(t)
	adminID, keyID := seedAdminAndKey(t, s)
	seedAccounts(t, s, "free", 1)
	ctx := context.Background()
	dispatched, err := s.DispatchAccounts(ctx, keyID, "delete-account", 1, time.Hour, "127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	if err = s.DeleteAccount(ctx, dispatched[0].ID); err != nil {
		t.Fatal(err)
	}
	var count int
	if err = s.DB.QueryRowContext(ctx, `SELECT count(*) FROM claude_account_dispatches WHERE account_id=?`, dispatched[0].ID).Scan(&count); err != nil || count != 0 {
		t.Fatalf("dispatch history was not deleted: count=%d err=%v", count, err)
	}
	if err = s.DeleteAccount(ctx, dispatched[0].ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("deleting missing account error=%v want ErrNotFound", err)
	}

	seedAccounts(t, s, "max_20x", 1)
	order, err := s.CreateOrder(ctx, Order{BatchNo: "delete-preserve-order", Buyer: "buyer", Quantity: 1, Plan: "max_20x"}, adminID)
	if err != nil {
		t.Fatal(err)
	}
	accounts, err := s.OrderAccounts(ctx, order.ID)
	if err != nil || len(accounts) != 1 {
		t.Fatalf("order accounts: %#v %v", accounts, err)
	}
	if err = s.DeleteAccount(ctx, accounts[0].ID); err == nil {
		t.Fatal("expected an order-linked account deletion to be rejected")
	}
}

func TestResetAccountRestoresFreeStateAndPreservesOrderAccounts(t *testing.T) {
	s := testStore(t)
	adminID, keyID := seedAdminAndKey(t, s)
	seedAccounts(t, s, "free", 1)
	ctx := context.Background()
	dispatched, err := s.DispatchAccounts(ctx, keyID, "reset-account", 1, time.Hour, "127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.UpgradeAccount(ctx, dispatched[0].Mail, "max_20x", time.Now(), 202); err != nil {
		t.Fatal(err)
	}
	if err = s.ResetAccount(ctx, dispatched[0].ID); err != nil {
		t.Fatal(err)
	}
	var plan, deliveryStatus, lockRequestID string
	var upgradedAt, cardPoolID, lockedUntil, deliveredAt any
	if err = s.DB.QueryRowContext(ctx, `SELECT plan,delivery_status,lock_request_id,upgraded_at,card_pool_id,locked_until,delivered_at FROM claude_accounts WHERE id=?`, dispatched[0].ID).Scan(&plan, &deliveryStatus, &lockRequestID, &upgradedAt, &cardPoolID, &lockedUntil, &deliveredAt); err != nil {
		t.Fatal(err)
	}
	if plan != "free" || deliveryStatus != "available" || lockRequestID != "" || upgradedAt != nil || cardPoolID != nil || lockedUntil != nil || deliveredAt != nil {
		t.Fatalf("account was not fully reset: plan=%s delivery=%s lock=%q upgraded=%v cardPoolID=%v locked=%v delivered=%v", plan, deliveryStatus, lockRequestID, upgradedAt, cardPoolID, lockedUntil, deliveredAt)
	}

	seedAccounts(t, s, "max_20x", 1)
	order, err := s.CreateOrder(ctx, Order{BatchNo: "reset-preserve-order", Buyer: "buyer", Quantity: 1, Plan: "max_20x"}, adminID)
	if err != nil {
		t.Fatal(err)
	}
	accounts, err := s.OrderAccounts(ctx, order.ID)
	if err != nil || len(accounts) != 1 {
		t.Fatalf("order accounts: %#v %v", accounts, err)
	}
	if err = s.ResetAccount(ctx, accounts[0].ID); err == nil {
		t.Fatal("expected an order-linked account reset to be rejected")
	}
}

func TestCancelBeforeDownloadReleasesAccounts(t *testing.T) {
	s := testStore(t)
	adminID, _ := seedAdminAndKey(t, s)
	seedAccounts(t, s, "free", 1)
	ctx := context.Background()
	order, err := s.CreateOrder(ctx, Order{BatchNo: "batch-cancel", Buyer: "buyer", Quantity: 1, Plan: "free"}, adminID)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.CancelOrder(ctx, order.ID); err != nil {
		t.Fatal(err)
	}
	next, err := s.CreateOrder(ctx, Order{BatchNo: "batch-next", Buyer: "buyer", Quantity: 1, Plan: "free"}, adminID)
	if err != nil || next.Status != "allocated" {
		t.Fatalf("released account was not reusable: %v", err)
	}
}
