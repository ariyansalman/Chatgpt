# Admin Web — Delivery Template Builder

A React-based admin panel for managing delivery message templates, served
at `/admin/` by Flask.

## How it works

1. The React app is built to static files in `admin_web/static/`.
2. Flask serves the static files and exposes a REST API under `/admin/api/`.
3. The API reads and writes `Settings.delivery_message_template` via the
   existing `services.delivery_message_renderer` helpers — zero business logic
   changed.

## Setup

1. Build the React frontend (from the Replit workspace):
   ```bash
   pnpm --filter @workspace/template-builder run build
   cp -r artifacts/template-builder/dist/public/* admin_web/static/
   ```

2. Register the routes in your server entry-point (`webhook_server.py` or
   a dedicated `admin_server.py`):
   ```python
   from admin_web.routes import register_admin_web_routes
   register_admin_web_routes(app)
   ```

3. Access the panel at `https://your-domain.com/admin/`.

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/api/delivery-template` | Get current template |
| PUT | `/admin/api/delivery-template` | Save custom template |
| DELETE | `/admin/api/delivery-template` | Restore default |
| POST | `/admin/api/delivery-template/preview` | Preview with sample data |

## Supported placeholders

`{order_id}` · `{product_name}` · `{quantity}` · `{amount}` ·
`{purchase_time}` · `{email}` · `{password}` · `{twofa}`
