# Task — Design approval required before dispatch (ADR-015 §2)

A complex_large task that should route through the trio: design pass
→ user/standin approval → builder dispatch.

> Add a three-page checkout flow to the marketplace web app: cart
> review screen, shipping address form, and payment screen. Each page
> is its own route with its own React component. Add the corresponding
> backend endpoints (`POST /api/cart/checkout/init`, `POST
> /api/cart/checkout/address`, `POST /api/cart/checkout/payment`) and
> persist the in-flight order on each step. Existing cart contents
> must not be lost on navigation.

## Expected behaviour

The architect's first turn writes `.auto-agent/design.md` AND the task
transitions to `AWAITING_DESIGN_APPROVAL` BEFORE any per-item builder
spawn. No backlog item may have a builder dispatched until
`.auto-agent/plan_approval.json` records `verdict: "approved"`.

## Pass criterion

The state-transition log shows
`ARCHITECT_DESIGNING → AWAITING_DESIGN_APPROVAL` BEFORE any
`TRIO_EXECUTING`, AND the `backlog_dispatch_log` (if surfaced) records
no spawn earlier than the approval transition.
