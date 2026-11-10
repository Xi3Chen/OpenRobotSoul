# Design

## Source of truth

Status: Draft
Date: 2026-11-07
Product surfaces: local robot runtime, LAN operator Dashboard, later mobile voice session.
Evidence reviewed: `README.md`, `docs/robot-agent-plan/01-architecture.md`, `02-adapter-contract.md`, `04-transaction-loop.md`, and `.omx/plans/prd-dashboard-sdk-risk.md`. The repository has no existing RobotHarness product UI. PicoClaw's bundled web UI is for coding-agent setup and is not part of the robot control product.

## Brand

The Dashboard is an operational instrument for a person supervising one robot. It should feel precise, calm and accountable. Connection state, robot identity, control authority and action risk are visible. Avoid marketing language, decorative hero layouts, playful motion, or visual styling that makes a disabled control appear available.

## Product goals

- Let an operator inspect robot health, current work, supported capabilities and recent outcomes from a phone or desktop on the local network.
- Route every command through the same typed runtime and local safety policy.
- Make AI review, operator authorization and robot execution distinct states.
- Explain why an operation is unavailable and show its parameter limits before submission.

Non-goals for the first Dashboard slice: arbitrary SDK console, low-level motor controls, public/cloud access, or claiming voice call support before a local speech and robot-audio path exists.

Success signals: denied and stale operations remain unexecuted; operators can identify robot readiness and action status at a glance; high-risk controls are absent unless backend policy grants them.

## Personas and jobs

Primary persona: robot operator or maintainer using a phone near the robot or a desktop on the same LAN. Jobs: check connection and health, issue a bounded supported skill, review why a skill is gated, inspect a proposed action and its review result, and inspect recent audit outcomes.

## Information architecture

- Overview: robot identity, connection/health, current task and latest feedback.
- Controls: authorized typed operation forms and text task entry.
- Capabilities: risk, callers, parameter constraints, state and unavailable reason.
- Activity: action, review and audit timeline.
- Authentication: pairing/login and operator session state.

Voice interaction is a later surface under Controls. It must share the runtime policy path and use secure browser media APIs.

## Design principles

- Put robot identity and live readiness before controls.
- Show the current operation, exact parameters and consequences before any gated action.
- Use persistent risk labels and explicit disabled reasons; color never carries meaning alone.
- Prefer a dense, scan-friendly layout with direct navigation and few modal steps.
- Treat errors and missing telemetry as unknown, never as healthy.

## Visual language

Use a neutral white/light-gray work surface with charcoal text, restrained green for healthy, amber for review/attention, red for denied/high-impact, and cyan/blue as a limited interaction accent. Pair every color with a label or icon and maintain WCAG AA contrast. Use system sans-serif typography, a 4/8px spacing scale, square-to-4px corners, thin separators and minimal elevation. Avoid oversized display type, gradients, nested cards and decorative imagery.

## Components

- Application shell with robot selector/identity, connection indicator and compact navigation.
- Status row and telemetry definition list for health and task state.
- Capability table/list with risk label, allowed callers, bounds, availability and reason.
- Bounded action form generated from typed parameter metadata.
- Review/authorization state row and explicit per-action confirmation dialog when policy requires it.
- Audit table with timestamp, action, actor/source, decision and outcome.
- Session/pairing form with visible session expiry and sign-out.

## Accessibility

Target WCAG 2.2 AA. Use semantic landmarks and headings, labels for all controls, keyboard access and visible focus. Announce action results and review changes through an accessible live region. Confirmation dialogs must trap focus and return it to the triggering control. Do not encode risk or connection state by color alone. Respect reduced-motion settings.

## Responsive behavior

Support phone widths from 320px, tablets and desktop. On phones, use a compact top bar and bottom or collapsible navigation, stack status values, use full-width touch targets, and render capability rows as readable definition blocks instead of a clipped wide table. Keep actions and warning text within the viewport. On desktop, use a narrow navigation rail and a wider data workspace.

## Interaction states

Define loading, no robot selected, disconnected, stale telemetry, unavailable capability, invalid parameters, review pending, review denied/uncertain, operator confirmation pending, execution in progress, completed, failed, expired session, offline/reconnecting, and audit unavailable. Never optimistically report physical success before adapter feedback supports it.

## Content voice

Use concise, factual Chinese labels and messages. Name the robot, operation, parameters and reason. Distinguish “AI 复核通过” from “操作员已确认” and “机器人已执行”. Avoid claiming that command acceptance means action completion.

## Implementation constraints

Initial implementation should remain dependency-light and serve a static responsive UI from the Python runtime. The server owns caller identity, session checks and risk policy. Bind to loopback by default; require HTTPS before binding to a LAN interface. Mobile microphone access requires HTTPS and a separately configured local speech transport. Do not expose DDS, arbitrary SDK calls, or raw driver commands to the browser. Verify API authorization, phone/desktop layout, keyboard navigation and action denial paths before hardware use.

## Open questions

- [ ] Which local speech-to-text/text-to-speech provider and robot audio transport will support a true phone-to-robot voice session? Owner: product/runtime. Impact: required before enabling microphone interaction.
- [ ] Which SDK and firmware revision will define the first complete method-policy inventory? Owner: robot integration. Impact: required before enabling any Go2 skill.
- [ ] Which certificate/pairing workflow will be used on operator phones? Owner: deployment. Impact: required for LAN HTTPS rollout.
