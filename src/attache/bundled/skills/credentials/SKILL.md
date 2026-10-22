---
name: credentials
description: Explain model API key setup and credential availability when the owner wants to use an OS keyring or migrate an endpoint from an environment variable.
---
# Model credentials

The owner manages model keys in their own terminal. Never ask for an API key in chat, put
one in a tool argument, or run login/logout for them. This skill is not available to members.

First define the endpoint in `attache.toml`: `api`, `model`, and the provider's `base_url`.
Then tell the owner to run:

```console
attache skill credentials login custom
attache skill credentials status custom
attache doctor
```

Replace `custom` with the endpoint name. `login` shows the destination, reads hidden input,
saves a destination-bound credential in the OS store, and updates only the endpoint's
credential reference while preserving the rest of the configuration. It replaces `key_env`
with `key_ref` after successful storage. Restart Attaché afterwards.

`status` and `doctor` check local availability, not provider authentication. A keyring may
ask the owner to unlock or authorize access. Configure `key_env` instead for a service
without an accessible OS store; never configure both sources or use a plaintext fallback.

To replace a key, run `login` again. To delete it locally:

```console
attache skill credentials logout custom
```

`logout` keeps `key_ref`, so new clients fail clearly until the owner logs in again. Restart
Attaché to discard cached clients. Local deletion does not revoke the key at its provider.
A manually shared key_ref is deleted for every endpoint that refers to it.

Changing the API base URL requires login again. Use the final URL: keyring clients refuse
HTTP redirects. Changing a model or renaming an endpoint
preserves access through its reference. A copied configuration has no secret in it; on a
different machine, run login again. The store protects persistence, not against programs
with the owner's OS account access.
