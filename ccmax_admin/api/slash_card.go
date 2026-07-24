package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"ccmax/dao"

	"github.com/gin-gonic/gin"
)

const (
	slashVaultBaseURL        = "https://vault.slash.com"
	slashCCMaxAlias          = "ccmax"
	defaultSlashCardGroupID  = "card_group_3febhaydgdiq9"
	automaticSlashCardPrefix = "ccmax-auto"
)

type slashCreateCardInput struct {
	Source           string         `json:"source"`
	Name             string         `json:"name"`
	AccountID        string         `json:"accountId"`
	VirtualAccountID string         `json:"virtualAccountId"`
	CardGroupID      string         `json:"cardGroupId"`
	CardProductID    string         `json:"cardProductId"`
	LegalEntity      string         `json:"legalEntity"`
	IsSingleUse      bool           `json:"isSingleUse"`
	UserData         map[string]any `json:"userData"`
}

type slashCardDetails struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Last4       string `json:"last4"`
	ExpiryMonth string `json:"expiryMonth"`
	ExpiryYear  string `json:"expiryYear"`
	Status      string `json:"status"`
	PAN         string `json:"pan"`
	CVV         string `json:"cvv"`
}

func (card *slashCardDetails) UnmarshalJSON(data []byte) error {
	type plainCard slashCardDetails
	var direct plainCard
	if err := json.Unmarshal(data, &direct); err != nil {
		return err
	}
	*card = slashCardDetails(direct)
	var wrappers map[string]json.RawMessage
	if err := json.Unmarshal(data, &wrappers); err != nil {
		return err
	}
	for _, key := range []string{"card", "data", "result"} {
		raw := wrappers[key]
		if len(raw) == 0 || string(raw) == "null" {
			continue
		}
		var nested slashCardDetails
		if err := json.Unmarshal(raw, &nested); err == nil {
			card.mergeMissing(nested)
		}
	}
	return nil
}

func (card *slashCardDetails) mergeMissing(other slashCardDetails) {
	card.ID = firstNonEmpty(card.ID, other.ID)
	card.Name = firstNonEmpty(card.Name, other.Name)
	card.Last4 = firstNonEmpty(card.Last4, other.Last4)
	card.ExpiryMonth = firstNonEmpty(card.ExpiryMonth, other.ExpiryMonth)
	card.ExpiryYear = firstNonEmpty(card.ExpiryYear, other.ExpiryYear)
	card.Status = firstNonEmpty(card.Status, other.Status)
	card.PAN = firstNonEmpty(card.PAN, other.PAN)
	card.CVV = firstNonEmpty(card.CVV, other.CVV)
}

type slashCardGroupList struct {
	Items []struct {
		ID   string `json:"id"`
		Name string `json:"name"`
	} `json:"items"`
}

type slashCardProductList struct {
	Items []struct {
		ID     string `json:"id"`
		Prefix string `json:"prefix"`
		Status string `json:"status"`
	} `json:"items"`
}

type slashCardImportResult struct {
	LocalID int64
	Last4   string
	Name    string
}

type slashCardImportError struct {
	err error
}

func (e *slashCardImportError) Error() string { return e.err.Error() }
func (e *slashCardImportError) Unwrap() error { return e.err }

func (s *Server) createSlashCard(c *gin.Context) {
	var input slashCreateCardInput
	if !bind(c, &input) {
		return
	}
	input.Source = strings.ToLower(strings.TrimSpace(input.Source))
	if input.Source == "" {
		input.Source = "slash"
	}
	if !strings.HasPrefix(input.Source, "slash") {
		fail(c, http.StatusBadRequest, "BAD_REQUEST", errors.New("source must start with slash"))
		return
	}
	input.Name = strings.TrimSpace(input.Name)
	if input.Name == "" {
		fail(c, http.StatusBadRequest, "BAD_REQUEST", errors.New("name is required"))
		return
	}
	applySlashCardDefaults(&input)
	token, err := s.store.Credential(c.Request.Context(), input.Source)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	if strings.EqualFold(strings.TrimSpace(input.CardGroupID), slashCCMaxAlias) {
		input.CardGroupID, err = resolveSlashCardGroupAlias(c.Request.Context(), s.slashBaseURL, token, input.LegalEntity, slashCCMaxAlias)
		if err != nil {
			fail(c, http.StatusBadGateway, "UPSTREAM_ERROR", err)
			return
		}
		if input.CardGroupID == "" {
			fail(c, http.StatusBadRequest, "BAD_REQUEST", errors.New(`Slash card group with ID or name "ccmax" was not found`))
			return
		}
	}
	if strings.EqualFold(strings.TrimSpace(input.CardProductID), slashCCMaxAlias) {
		input.CardProductID, err = resolveSlashCardProductAlias(c.Request.Context(), s.slashBaseURL, token, input.LegalEntity, slashCCMaxAlias)
		if err != nil {
			fail(c, http.StatusBadGateway, "UPSTREAM_ERROR", err)
			return
		}
		if input.CardProductID == "" {
			fail(c, http.StatusBadRequest, "BAD_REQUEST", errors.New(`active Slash card product with prefix "ccmax" was not found`))
			return
		}
	}
	created, err := createCardAtSlash(c.Request.Context(), s.slashBaseURL, token, input)
	if err != nil {
		fail(c, http.StatusBadGateway, "UPSTREAM_ERROR", err)
		return
	}
	result, err := s.importCreatedSlashCard(c.Request.Context(), input.Source, token, input.LegalEntity, created.ID)
	if err != nil {
		var importErr *slashCardImportError
		if errors.As(err, &importErr) {
			fail(c, http.StatusBadGateway, "UPSTREAM_ERROR", fmt.Errorf("Slash card %s was created but could not be imported: %w", created.ID, err))
		} else {
			handleStoreError(c, err)
		}
		return
	}
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "create_slash_card", "card", strconv.FormatInt(result.LocalID, 10), fmt.Sprintf(`{"source":%q,"slashCardId":%q,"name":%q}`, input.Source, created.ID, created.Name), clientIP(c))
	ok(c, gin.H{"id": result.LocalID, "source": input.Source, "cardId": created.ID, "last4": firstNonEmpty(result.Last4, created.Last4), "name": firstNonEmpty(result.Name, created.Name)})
}

func applySlashCardDefaults(input *slashCreateCardInput) {
	if strings.TrimSpace(input.CardGroupID) == "" {
		input.CardGroupID = defaultSlashCardGroupID
	}
}

func (s *Server) createSlashCardForDispatch(ctx context.Context, requestedSource string) error {
	source := strings.ToLower(strings.TrimSpace(requestedSource))
	if source == "" {
		var err error
		source, err = s.store.CredentialSourceByPrefix(ctx, "slash")
		if err != nil {
			return err
		}
	}
	if !strings.HasPrefix(source, "slash") {
		return errors.New("automatic card creation requires a Slash source")
	}
	token, err := s.store.Credential(ctx, source)
	if err != nil {
		return err
	}
	input := slashCreateCardInput{
		Source: source,
		Name:   automaticSlashCardPrefix + "-" + randomToken(8),
	}
	applySlashCardDefaults(&input)
	created, err := createCardAtSlash(ctx, s.slashBaseURL, token, input)
	if err != nil {
		return err
	}
	_, err = s.importCreatedSlashCard(ctx, source, token, input.LegalEntity, created.ID)
	return err
}

func (s *Server) importSlashCardByID(c *gin.Context) {
	var input struct {
		Source      string `json:"source"`
		CardID      string `json:"cardId"`
		LegalEntity string `json:"legalEntity"`
	}
	if !bind(c, &input) {
		return
	}
	input.Source = strings.ToLower(strings.TrimSpace(input.Source))
	if input.Source == "" {
		input.Source = "slash"
	}
	input.CardID = strings.TrimSpace(input.CardID)
	if !strings.HasPrefix(input.Source, "slash") || input.CardID == "" {
		fail(c, http.StatusBadRequest, "BAD_REQUEST", errors.New("slash source and cardId are required"))
		return
	}
	token, err := s.store.Credential(c.Request.Context(), input.Source)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	result, err := s.importSlashCard(c.Request.Context(), input.Source, token, input.LegalEntity, input.CardID)
	if err != nil {
		var importErr *slashCardImportError
		if errors.As(err, &importErr) {
			fail(c, http.StatusBadGateway, "UPSTREAM_ERROR", err)
		} else {
			handleStoreError(c, err)
		}
		return
	}
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "import_slash_card", "card", strconv.FormatInt(result.LocalID, 10), fmt.Sprintf(`{"source":%q,"slashCardId":%q}`, input.Source, input.CardID), clientIP(c))
	ok(c, gin.H{"id": result.LocalID, "source": input.Source, "cardId": input.CardID, "last4": result.Last4, "name": result.Name})
}

func (s *Server) importCreatedSlashCard(ctx context.Context, source, token, legalEntity, cardID string) (*slashCardImportResult, error) {
	ctx, cancel := context.WithTimeout(ctx, s.slashImportTimeout)
	defer cancel()

	var lastErr error
	for {
		result, err := s.importSlashCard(ctx, source, token, legalEntity, cardID)
		if err == nil {
			return result, nil
		}
		var importErr *slashCardImportError
		if !errors.As(err, &importErr) {
			return nil, err
		}
		lastErr = err

		timer := time.NewTimer(s.slashImportRetryInterval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, &slashCardImportError{err: fmt.Errorf("timed out after %s waiting for card details: %w", s.slashImportTimeout, lastErr)}
		case <-timer.C:
		}
	}
}

func (s *Server) importSlashCard(ctx context.Context, source, token, legalEntity, cardID string) (*slashCardImportResult, error) {
	details, err := fetchSlashCardDetails(ctx, s.slashBaseURL, token, legalEntity, cardID)
	if err != nil {
		return nil, &slashCardImportError{err: fmt.Errorf("read Slash card %s details: %w", cardID, err)}
	}
	secrets, err := fetchSlashCardSecrets(ctx, s.slashVaultURL, token, legalEntity, cardID)
	if err != nil {
		return nil, &slashCardImportError{err: fmt.Errorf("read Slash card %s PAN/CVV: %w", cardID, err)}
	}
	cardNo := compactDigits(firstNonEmpty(secrets.PAN, details.PAN))
	ccv := compactDigits(firstNonEmpty(secrets.CVV, details.CVV))
	expireMMYY, expiryErr := slashExpireMMYY(firstNonEmpty(secrets.ExpiryMonth, details.ExpiryMonth), firstNonEmpty(secrets.ExpiryYear, details.ExpiryYear))
	missing := missingSlashCardFields(cardNo, ccv, expiryErr)
	if len(missing) > 0 {
		return nil, &slashCardImportError{err: fmt.Errorf("Slash card %s returned incomplete card details (missing %s)", cardID, strings.Join(missing, ", "))}
	}
	status := 1
	if !strings.EqualFold(firstNonEmpty(secrets.Status, details.Status), "active") {
		status = -1
	}
	localID, err := s.store.CreateCard(ctx, dao.Card{
		Source: source, CardID: cardID, CardNo: cardNo, ExpireMMYY: expireMMYY, CCV: ccv, Status: status,
	})
	if err != nil {
		return nil, err
	}
	return &slashCardImportResult{LocalID: localID, Last4: firstNonEmpty(secrets.Last4, details.Last4), Name: details.Name}, nil
}

func missingSlashCardFields(cardNo, ccv string, expiryErr error) []string {
	missing := make([]string, 0, 3)
	if cardNo == "" {
		missing = append(missing, "PAN")
	}
	if ccv == "" {
		missing = append(missing, "CVV")
	}
	if expiryErr != nil {
		missing = append(missing, "expiry")
	}
	return missing
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value = strings.TrimSpace(value); value != "" {
			return value
		}
	}
	return ""
}

func resolveSlashCardGroupAlias(ctx context.Context, baseURL, apiKey, legalEntity, alias string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(baseURL, "/")+"/card-group", nil)
	if err != nil {
		return "", err
	}
	setSlashAPIHeaders(req, apiKey, legalEntity)
	var result slashCardGroupList
	if err = doSlashCardRequest(req, http.StatusOK, &result); err != nil {
		return "", err
	}
	for _, item := range result.Items {
		if strings.EqualFold(strings.TrimSpace(item.Name), alias) || strings.EqualFold(strings.TrimSpace(item.ID), alias) {
			return strings.TrimSpace(item.ID), nil
		}
	}
	return "", nil
}

func resolveSlashCardProductAlias(ctx context.Context, baseURL, apiKey, legalEntity, alias string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(baseURL, "/")+"/card-product", nil)
	if err != nil {
		return "", err
	}
	setSlashAPIHeaders(req, apiKey, legalEntity)
	var result slashCardProductList
	if err = doSlashCardRequest(req, http.StatusOK, &result); err != nil {
		return "", err
	}
	for _, item := range result.Items {
		matches := strings.EqualFold(strings.TrimSpace(item.Prefix), alias) || strings.EqualFold(strings.TrimSpace(item.ID), alias)
		active := strings.TrimSpace(item.Status) == "" || strings.EqualFold(strings.TrimSpace(item.Status), "active")
		if matches && active {
			return strings.TrimSpace(item.ID), nil
		}
	}
	return "", nil
}

func createCardAtSlash(ctx context.Context, baseURL, apiKey string, input slashCreateCardInput) (*slashCardDetails, error) {
	payload := map[string]any{"type": "virtual", "name": input.Name, "isSingleUse": input.IsSingleUse}
	optional := map[string]string{
		"accountId":        input.AccountID,
		"virtualAccountId": input.VirtualAccountID,
		"cardGroupId":      input.CardGroupID,
		"cardProductId":    input.CardProductID,
	}
	for key, value := range optional {
		if value = strings.TrimSpace(value); value != "" {
			payload[key] = value
		}
	}
	if len(input.UserData) > 0 {
		payload["userData"] = input.UserData
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(baseURL, "/")+"/card", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	setSlashAPIHeaders(req, apiKey, input.LegalEntity)
	req.Header.Set("Content-Type", "application/json")
	var card slashCardDetails
	if err = doSlashCardRequest(req, http.StatusCreated, &card); err != nil {
		return nil, err
	}
	if strings.TrimSpace(card.ID) == "" {
		return nil, errors.New("Slash create-card response did not include id")
	}
	return &card, nil
}

func fetchSlashCardSecrets(ctx context.Context, vaultBaseURL, apiKey, legalEntity, cardID string) (*slashCardDetails, error) {
	path := "/card/" + url.PathEscape(strings.TrimSpace(cardID)) + "?include_pan=true&include_cvv=true"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(vaultBaseURL, "/")+path, nil)
	if err != nil {
		return nil, err
	}
	setSlashAPIHeaders(req, apiKey, legalEntity)
	var card slashCardDetails
	if err = doSlashCardRequest(req, http.StatusOK, &card); err != nil {
		return nil, err
	}
	return &card, nil
}

func fetchSlashCardDetails(ctx context.Context, baseURL, apiKey, legalEntity, cardID string) (*slashCardDetails, error) {
	path := "/card/" + url.PathEscape(strings.TrimSpace(cardID))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(baseURL, "/")+path, nil)
	if err != nil {
		return nil, err
	}
	setSlashAPIHeaders(req, apiKey, legalEntity)
	var card slashCardDetails
	if err = doSlashCardRequest(req, http.StatusOK, &card); err != nil {
		return nil, err
	}
	if strings.TrimSpace(card.ID) == "" {
		return nil, errors.New("Slash card-details response did not include id")
	}
	return &card, nil
}

func setSlashAPIHeaders(req *http.Request, apiKey, legalEntity string) {
	req.Header.Set("Accept", "application/json")
	req.Header.Set("X-API-Key", strings.TrimSpace(apiKey))
	if legalEntity = strings.TrimSpace(legalEntity); legalEntity != "" {
		req.Header.Set("X-Legal-Entity", legalEntity)
	}
}

func doSlashCardRequest(req *http.Request, wantStatus int, target any) error {
	resp, err := (&http.Client{Timeout: 15 * time.Second}).Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode != wantStatus {
		return fmt.Errorf("Slash HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	if err = json.Unmarshal(raw, target); err != nil {
		return fmt.Errorf("decode Slash response: %w", err)
	}
	return nil
}

func compactDigits(value string) string {
	var result strings.Builder
	for _, char := range value {
		if char >= '0' && char <= '9' {
			result.WriteRune(char)
		}
	}
	return result.String()
}

func slashExpireMMYY(month, year string) (string, error) {
	month = compactDigits(month)
	year = compactDigits(year)
	if len(month) == 1 {
		month = "0" + month
	}
	if len(month) != 2 || len(year) < 2 {
		return "", errors.New("invalid Slash expiry")
	}
	return month + year[len(year)-2:], nil
}
