"""Local subscriber registry: no external relay, shared DB only within one tenant."""
import os
from pathlib import Path
import monitor
from deploy.relay import RelayStore, RelayApplication, _token_hash


def local_state_request(payload):
    tenant = os.environ['MONITOR_TENANT']
    token = os.environ['TELEGRAM_BOT_TOKEN']
    secret = os.environ['RELAY_PATH_SECRET']
    store = RelayStore(Path(os.environ['RELAY_DATABASE']))
    store.register_token(tenant, _token_hash(token))
    store.mark_webhook_configured(tenant)
    app = RelayApplication(store, 'https://localhost', {tenant: secret})
    status, result = app.handle('/v1/' + tenant + '/' + secret,
        {'X-DAR-Bot-Token': token}, payload)
    if status != 200 or not result.get('ok'):
        raise RuntimeError('Local subscriber registry failed')
    return result


monitor._telegram_state_request = local_state_request
if __name__ == '__main__':
    raise SystemExit(monitor.main())
