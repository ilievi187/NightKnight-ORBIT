# ORBIT Model Picker: Local + Cloud API

The ORBIT onboarding screen has three filters: **Моите локални**, **Cloud / API**,
**Всички**, plus model search and **Добави provider**.

Local discovery checks fixed **127.0.0.1 only** ports 18879, 8080, 11434, 1234,
5000, 8000, 8001, 3000 and 3900. It calls GET /v1/models and for Ollama also
GET /api/tags. Users can explicitly add additional local OpenAI-compatible ports.
No remote network/LAN scan or GPU/model auto-launch happens.

Cloud templates are OpenAI API, OpenRouter API, Groq API, Gemini API's OpenAI
compatibility endpoint, Mistral API and Cerebras API. The user supplies an
actual API credential through setup. Credentials are written only on the local
machine into data_dir/provider-sources.json with mode 0600, not checked in,
included in metrics, or sent back by any API response. The provider's credential
is used server-side for model discovery and chat completions.

**A subscription to ChatGPT Plus/Pro, Gemini Advanced or Claude is not the same
as their developer API quota**, and ORBIT does not borrow or scrape the login
session from those apps. A connected compatible gateway can be configured as
a local provider; API credentials may have separate costs.

Cloud endpoints are limited to an allowlisted official HTTPS host/path; user
supplied HTTP endpoints must be loopback-only. The model picker is a catalog
of IDs available through a responding /models endpoint, not proof that weights
are loaded or that a generation request succeeds. Cloud providers lacking keys
display 'Изисква API key'; other unresponsive sources stay offline. No
invented paid subscription entitlements or worker capabilities.

**Security caveat:** The desktop MVP has no authenticated remote operator API
yet. Keep ORBIT bound to 127.0.0.1 and do not add Tailscale or LAN forwarding
until the operator control plane is unified and authenticated (PRs #14/#15).
For production hardening, consider OS keyring encrypted credential storage.
