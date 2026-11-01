# ORBIT: First-run Brain -> Workers -> Mission Control

The first screen asks for the brain model. A model is never preselected by the
interface. Local OpenAI-compatible model endpoints are checked only on configured
URLs and fixed 127.0.0.1 ports 18879, 8080, 11434. No GPU process is launched.

The user's selected live model ID and endpoint are saved in SQLite. The existing
chat Harness then sends its completions to the selected model endpoint. Online
means a model-list API response, not verified generation or loaded GPU weights.

Second, the user selects which real, freshly heartbeating workers ORBIT may
assign tasks. Initially there are zero workers and no imaginary ALEX/NOVA/HERMES.
The user can continue with zero workers, for chat-only mode, and add them later
from FLEET. Plan Mode eligible workers are filtered by this explicit allowlist
after onboarding. Selecting a new brain resets previous worker permissions.

Existing session history is retained. No old agent session is automatically
restored as the first active session.

Security limitation: The MVP remains loopback-only. The unified authenticated
operator control plane and canonical Grok/Antigravity worker broker are pending.
Do not expose these endpoints via LAN/Tailscale until operator authorization
and permission audits are implemented. This is not yet a remotely authorized
fleet orchestration product.
