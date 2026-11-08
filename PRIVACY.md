# PayLock Privacy Policy

**Last updated:** November 8, 2026

PayLock is a Telegram Mini App escrow marketplace. Buyers fund a wallet, funds are held in escrow, the seller delivers, the buyer confirms, and funds are released to the seller.

By using PayLock, you agree to this Privacy Policy.

## 1. Who we are

PayLock is operated through a Telegram Bot / Mini App. For privacy questions, contact us via the Telegram Bot that opened this Mini App.

## 2. Data we collect

We only collect what is needed to run the escrow marketplace:

**From Telegram (via Telegram WebApp initData):**
- Telegram user ID
- First name, last name (if provided by Telegram)
- Username (if provided by Telegram)
- Profile photo URL (if provided by Telegram)
- Language code

We verify this data using HMAC SHA256 with our Bot Token. We do not access your Telegram messages, contacts, or phone number from Telegram unless you provide it.

**You provide directly:**
- Product listings: title, description, price, cover image, optional product file
- Bank details for withdrawal: bank name, account number, account name (for verification)
- 4-digit PIN (stored only as a one-way hash, never in plain text)
- Support messages you send us

**Automatically collected:**
- Wallet transactions, escrow orders, withdrawals
- IP address, device / Telegram client info, and app logs for security and fraud prevention

## 3. Payments

Wallet funding and withdrawals are processed by **Paystack**.

We do not store your full card number, CVV, or card PIN. Paystack may store a saved-card token / authorization code to allow saved-card payments, as described in their own privacy policy.

## 4. How we use your data

- To create and manage your account
- To hold funds in escrow and release them after delivery confirmation
- To process wallet funding and withdrawals
- To verify your bank account before withdrawal
- To prevent fraud, enforce limits, and secure your PIN
- To provide support and resolve disputes
- To comply with legal obligations

## 5. Product files and storage

- Product cover images and product files are stored in Supabase Storage.
- Product files are intended only for the buyer after purchase. We are moving product files to a private bucket with signed URLs. Until then, do not upload files containing passwords, private keys, or highly sensitive personal data.
- We do not sell your data.

## 6. Sharing

We share data only as needed to operate PayLock:
- **Supabase:** database and file storage
- **Paystack:** payments, bank verification, and withdrawals
- **Telegram:** Mini App authentication
- **Hosting provider (Render):** app hosting and logs

We do not sell personal data to third parties.

## 7. Retention

- Wallet transaction history is kept for accounting, fraud prevention, and legal compliance, even after account deletion.
- Product listings are hidden on account deletion.
- Bank details and personal identifiers are anonymized on account deletion where legally allowed.
- Logs are kept for a limited period for security.

## 8. Delete Account

You can delete your account inside the app: `DELETE /api/account`

On deletion:
- Your account is soft-deleted
- Your listings are hidden
- Your email / phone / account name are anonymized
- Transaction history is kept for accounting and fraud prevention

You cannot delete your account while you have a balance above zero or an open escrow order (held or disputed). Withdraw or complete / cancel those first.

## 9. Security

- Telegram initData is verified with HMAC SHA256 on every request
- PIN is hashed, 5 wrong attempts lock withdrawals / deletion for 15 minutes
- Bank account must be verified before withdrawal
- Access to production keys is restricted

No system is 100% secure. If you believe your account is compromised, contact us immediately via the Bot.

## 10. Children

PayLock is not intended for users under 18. If you are under 18, do not use PayLock.

## 11. Your rights

You may request:
- A copy of your data
- Correction of inaccurate data
- Deletion of your account (subject to Section 8)

Contact us via the Telegram Bot that opened this Mini App.

## 12. Changes

We may update this policy. The updated version will be published in-app and in this file with a new “Last updated” date. Continued use after changes means you accept the new policy.

## 13. Telegram

PayLock runs inside Telegram as a Mini App. Telegram’s own Privacy Policy also applies to your use of Telegram. PayLock does not control Telegram’s data practices.
