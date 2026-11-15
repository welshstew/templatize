// Built-in sample documents for the "Samples" picker. All data is fictional:
// example.* domains, RFC 5737 / RFC 1918 addresses, 555 phone numbers.
// Samples with `variants` show the multi-example diff; the rest show what the
// heuristics (and NER/LLM, if enabled) find in a single document. A sample with an
// `engine` selects that template engine when loaded (default: jinja2).

export interface Sample {
  id: string;
  label: string;
  group: "Config & infrastructure" | "Documents & messages" | "Data & code";
  text: string;
  variants?: string[];
  engine?: "jinja2" | "envsubst" | "terraform";
}

const nginx = (host: string, ip: string, env: string) => `server {
    listen 443 ssl;
    server_name ${host}.example.com;

    ssl_certificate /etc/nginx/certs/${host}.example.com.crt;

    location / {
        proxy_pass ${ip}:8080;
        proxy_set_header X-Env ${env};
    }
}
`;

const k8s = (app: string, ns: string, replicas: number, tag: string, mem: string) => `apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${app}
  namespace: ${ns}
  labels:
    app: ${app}
spec:
  replicas: ${replicas}
  selector:
    matchLabels:
      app: ${app}
  template:
    metadata:
      labels:
        app: ${app}
    spec:
      containers:
        - name: ${app}
          image: registry.example.com/acme/${app}:${tag}
          ports:
            - containerPort: 8080
          env:
            - name: LOG_LEVEL
              value: info
          resources:
            limits:
              memory: ${mem}
              cpu: 500m
`;

const stack = (o: { env: string; ns: string; replicas: number; tag: string; level: string; host: string; mem: string }) => `# Storefront: ${o.env}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: storefront-config
  namespace: ${o.ns}
data:
  LOG_LEVEL: ${o.level}
  FEATURE_FLAGS: "checkout-v2,search-beta"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: storefront
  namespace: ${o.ns}
  labels:
    app.kubernetes.io/name: storefront
spec:
  replicas: ${o.replicas}
  selector:
    matchLabels:
      app.kubernetes.io/name: storefront
  template:
    metadata:
      labels:
        app.kubernetes.io/name: storefront
    spec:
      containers:
        - name: web
          image: ghcr.io/acme/storefront:${o.tag}
          ports:
            - containerPort: 8080
          envFrom:
            - configMapRef:
                name: storefront-config
          resources:
            limits:
              cpu: "1"
              memory: ${o.mem}
---
apiVersion: v1
kind: Service
metadata:
  name: storefront
  namespace: ${o.ns}
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: storefront
  ports:
    - port: 80
      targetPort: 8080
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: storefront
  namespace: ${o.ns}
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - ${o.host}
      secretName: storefront-tls
  rules:
    - host: ${o.host}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: storefront
                port:
                  number: 80
`;

const dotenv = (env: string, appUrl: string, dbHost: string, dbName: string, key: string, debug: string) => `APP_ENV=${env}
APP_URL=${appUrl}
DEBUG=${debug}

DB_HOST=${dbHost}
DB_PORT=5432
DB_NAME=${dbName}
DB_USER=app_user

REDIS_URL=redis://cache.internal.example.com:6379/0
STRIPE_API_KEY=${key}
SENTRY_DSN=https://abc123@sentry.example.com/42
`;

const systemd = (name: string, user: string, port: number, desc: string) => `[Unit]
Description=${desc}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${user}
WorkingDirectory=/opt/${name}
ExecStart=/opt/${name}/bin/${name} --port ${port} --config /etc/${name}/config.toml
Restart=on-failure
RestartSec=5
Environment=RUST_LOG=info

[Install]
WantedBy=multi-user.target
`;

const letter = (o: {
  date: string;
  name: string;
  title: string;
  company: string;
  street: string;
  city: string;
  po: string;
  amount: string;
  ship: string;
}) => `Northwind Components Ltd
14 Harbour Road
Bristol BS1 6AA

${o.date}

${o.name}
${o.title}
${o.company}
${o.street}
${o.city}

Dear ${o.name.split(" ")[0]},

Thank you for purchase order ${o.po}. We are pleased to confirm the order for a
total of ${o.amount}, payable within 30 days of invoice.

The goods will be dispatched on ${o.ship} from our Bristol warehouse. Your account
manager, Ravi Patel, can be reached on +44 117 555 0142 or at
ravi.patel@northwind.example.com.

Yours sincerely,

Helen Clarke
Sales Director
`;

const invoice = (o: { no: string; date: string; due: string; client: string; email: string; lines: string; total: string }) => `INVOICE ${o.no}

Issued: ${o.date}
Due:    ${o.due}

Bill to:
${o.client}
${o.email}

Description                         Qty    Unit price       Amount
------------------------------------------------------------------
${o.lines}
------------------------------------------------------------------
                                                  Total  ${o.total}

Pay by bank transfer to Brightline Studio, sort code 20-00-00, account 55512345,
quoting ${o.no} as the reference.
`;

const welcome = (name: string, email: string, plan: string, date: string, team: string) => `Subject: Welcome to Lumen, ${name}!

Hi ${name},

Your ${plan} account is ready. You can sign in at https://app.lumen.example.com
with ${email}.

Your free trial runs until ${date}. After that you'll be billed monthly unless you
cancel from Settings > Billing.

You've been added to the "${team}" workspace. Your workspace admin can invite more
people at any time.

Need help? Reply to this email or visit https://help.lumen.example.com for guides.

— The Lumen team
`;

export const SAMPLES: Sample[] = [
  // ---- Config & infrastructure ----
  {
    id: "nginx",
    label: "nginx server block (2 examples)",
    group: "Config & infrastructure",
    text: nginx("billing", "10.20.30.41", "production"),
    variants: [nginx("payments", "10.20.30.77", "staging")],
  },
  {
    id: "k8s",
    label: "Kubernetes Deployment (2 examples)",
    group: "Config & infrastructure",
    text: k8s("orders-api", "shop-prod", 3, "1.14.2", "512Mi"),
    variants: [k8s("inventory-api", "shop-staging", 1, "2.3.0", "256Mi")],
  },
  {
    id: "k8s-stack",
    label: "Kubernetes app stack → Helm chart (2 examples)",
    group: "Config & infrastructure",
    text: stack({ env: "production", ns: "shop-prod", replicas: 4, tag: "3.9.1", level: "info", host: "shop.example.com", mem: "1Gi" }),
    variants: [
      stack({ env: "staging", ns: "shop-staging", replicas: 1, tag: "3.10.0-rc.2", level: "debug", host: "shop.staging.example.com", mem: "512Mi" }),
    ],
  },
  {
    id: "compose",
    label: "Docker Compose stack",
    group: "Config & infrastructure",
    text: `services:
  web:
    image: ghcr.io/acme/storefront:4.2.1
    ports:
      - "8080:80"
    environment:
      DATABASE_URL: postgres://shop:s3cretPass@db:5432/storefront
      PUBLIC_HOST: shop.example.com
    depends_on: [db]

  db:
    image: postgres:16.4
    environment:
      POSTGRES_USER: shop
      POSTGRES_PASSWORD: s3cretPass
      POSTGRES_DB: storefront
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
`,
  },
  {
    id: "dotenv",
    label: ".env file (2 examples)",
    group: "Config & infrastructure",
    text: dotenv("production", "https://app.example.com", "db-prod-01.internal.example.com", "orders", "sk_live_EXAMPLE0000000000000000", "false"),
    variants: [dotenv("staging", "https://staging.example.com", "db-stg-01.internal.example.com", "orders_staging", "sk_test_EXAMPLE0000000000000000", "true")],
  },
  {
    id: "systemd",
    label: "systemd service unit (2 examples)",
    group: "Config & infrastructure",
    text: systemd("ingestd", "ingest", 9100, "Event ingestion daemon"),
    variants: [systemd("reportd", "reports", 9200, "Nightly report generator")],
  },
  {
    id: "terraform",
    label: "Terraform resources",
    group: "Config & infrastructure",
    text: `provider "aws" {
  region = "eu-west-2"
}

resource "aws_s3_bucket" "assets" {
  bucket = "acme-marketing-assets-prod"

  tags = {
    Team        = "marketing"
    Environment = "prod"
    CostCenter  = "CC-4410"
  }
}

resource "aws_instance" "web" {
  ami           = "ami-0a1b2c3d4e5f67890"
  instance_type = "t3.medium"
  subnet_id     = "subnet-0123abcd4567ef890"

  tags = {
    Name        = "marketing-web-01"
    Environment = "prod"
  }
}
`,
  },
  {
    id: "gha",
    label: "GitHub Actions workflow",
    group: "Config & infrastructure",
    text: `name: Deploy docs

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-24.04
    environment: production
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "22"
      - run: npm ci && npm run build
      - name: Upload
        run: rsync -az dist/ deploy@docs.example.com:/var/www/docs
        env:
          SSH_KNOWN_HOST: docs.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyOnly
`,
  },
  {
    id: "ssh",
    label: "SSH client config",
    group: "Config & infrastructure",
    text: `Host bastion
    HostName 203.0.113.10
    User jmorgan
    Port 2222
    IdentityFile ~/.ssh/id_ed25519_work

Host db-primary
    HostName 10.0.4.21
    User jmorgan
    ProxyJump bastion
    LocalForward 5433 127.0.0.1:5432
`,
  },
  {
    id: "ini",
    label: "INI application config",
    group: "Config & infrastructure",
    text: `[server]
host = 0.0.0.0
port = 8443
workers = 8
public_url = https://wiki.example.org

[database]
url = mysql://wiki:Tr0ub4dor@192.0.2.55:3306/wikidb
pool_size = 20

[mail]
smtp_host = smtp.example.org
smtp_port = 587
from_address = wiki-noreply@example.org
admin_email = it-support@example.org
`,
  },
  {
    id: "json",
    label: "JSON service config",
    group: "Config & infrastructure",
    text: `{
  "service": "notification-gateway",
  "version": "3.8.0",
  "listen": { "host": "0.0.0.0", "port": 7070 },
  "region": "us-east-1",
  "queue": {
    "url": "https://queue.us-east-1.example.com/123456789012/notifications",
    "batch_size": 25
  },
  "providers": {
    "sms": { "sender_id": "ACMEALERT", "api_key": "AKIAIOSFODNN7EXAMPLE" },
    "email": { "from": "alerts@acme.example.com", "reply_to": "support@acme.example.com" }
  },
  "owner": "platform-team@acme.example.com"
}
`,
  },

  // ---- Documents & messages ----
  {
    id: "letter",
    label: "Business letter (2 examples)",
    group: "Documents & messages",
    text: letter({
      date: "3 March 2026",
      name: "Sarah Okafor",
      title: "Procurement Manager",
      company: "Calder Engineering plc",
      street: "Unit 7, Riverside Park",
      city: "Leeds LS10 1AB",
      po: "PO-55821",
      amount: "GBP 18,450.00",
      ship: "17 March 2026",
    }),
    variants: [
      letter({
        date: "21 April 2026",
        name: "Tomasz Nowak",
        title: "Head of Operations",
        company: "Fenwick Robotics Ltd",
        street: "2 Mill Lane",
        city: "Manchester M4 2PL",
        po: "PO-56107",
        amount: "GBP 7,980.50",
        ship: "5 May 2026",
      }),
    ],
  },
  {
    id: "invoice",
    label: "Invoice (2 examples)",
    group: "Documents & messages",
    text: invoice({
      no: "INV-2026-0412",
      date: "2026-04-01",
      due: "2026-05-01",
      client: "Marlow & Hart Architects",
      email: "accounts@marlowhart.example.com",
      lines: `Website redesign                      1      4,200.00     4,200.00
Hosting (12 months)                   1        360.00       360.00`,
      total: "GBP 4,560.00",
    }),
    variants: [
      invoice({
        no: "INV-2026-0437",
        date: "2026-04-15",
        due: "2026-05-15",
        client: "Greenfield Bakery Co",
        email: "finance@greenfieldbakery.example.com",
        lines: `Brand identity package                1      2,750.00     2,750.00
Hosting (12 months)                   1        360.00       360.00`,
        total: "GBP 3,110.00",
      }),
    ],
  },
  {
    id: "welcome",
    label: "Welcome email (2 examples)",
    group: "Documents & messages",
    text: welcome("Priya", "priya.shah@example.com", "Team", "28 May 2026", "Design Ops"),
    variants: [welcome("Marcus", "marcus.lee@example.net", "Pro", "9 June 2026", "Growth Marketing")],
  },
  {
    id: "offer",
    label: "Job offer letter",
    group: "Documents & messages",
    text: `Dear Daniel Fischer,

We are delighted to offer you the position of Senior Data Engineer at Orbit
Analytics GmbH, reporting to Lena Brandt, Head of Data Platform.

Start date: 1 September 2026
Location: Berlin office (hybrid, 3 days on site)
Annual salary: EUR 92,000, paid monthly
Holiday: 28 days plus public holidays

This offer is conditional on satisfactory references. Please confirm your
acceptance by 15 July 2026 by replying to people@orbit-analytics.example.com.

Kind regards,

Jana Weber
People Partner, Orbit Analytics GmbH
`,
  },
  {
    id: "meeting",
    label: "Meeting invitation",
    group: "Documents & messages",
    text: `Subject: Q3 planning review — Thursday 9 July, 14:00 BST

Hi all,

Please join the Q3 planning review on Thursday 9 July from 14:00 to 15:30 BST in
the Aurora room (3rd floor) or on the video call:
https://meet.example.com/j/8841-2207-553

Agenda:
1. Q2 results — Amira Haddad (10 min)
2. Roadmap priorities — Chris Duval (40 min)
3. Hiring plan and budget of USD 240,000 — Amira Haddad (20 min)

Dial-in: +1 415 555 0199, PIN 66 41 09

Thanks,
Amira
`,
  },
  {
    id: "release",
    label: "Release notes (Markdown)",
    group: "Documents & messages",
    text: `# Harbor CLI 2.7.0

Released 12 August 2026.

## Highlights

- \`harbor deploy\` now supports \`--region eu-central-1\` and \`--region ap-south-1\`.
- Login tokens expire after 30 days instead of 7.

## Fixes

- Fixed a crash when \`HARBOR_HOME\` contained spaces (#4182).
- Uploads larger than 2 GiB no longer time out (#4199).

## Upgrade

    curl -fsSL https://get.harbor.example.com/install.sh | sh -s -- --version 2.7.0

Thanks to @lmartins and @kchen for their contributions.
`,
  },

  // ---- Data & code ----
  {
    id: "sql",
    label: "SQL seed script",
    group: "Data & code",
    text: `-- Seed tenant: Bluewater Logistics
INSERT INTO tenants (id, slug, name, plan, created_at)
VALUES ('7f3c2a10-5b1e-4d8a-9c77-2e4f6a1b0c93', 'bluewater', 'Bluewater Logistics', 'enterprise', '2026-02-14 09:30:00');

INSERT INTO users (tenant_id, email, role)
VALUES
  ('7f3c2a10-5b1e-4d8a-9c77-2e4f6a1b0c93', 'ops-admin@bluewater.example.com', 'admin'),
  ('7f3c2a10-5b1e-4d8a-9c77-2e4f6a1b0c93', 'dispatch@bluewater.example.com', 'member');

UPDATE tenants SET seat_limit = 150 WHERE slug = 'bluewater';
`,
  },
  {
    id: "html-email",
    label: "HTML password-reset email",
    group: "Data & code",
    text: `<!doctype html>
<html>
  <body style="font-family: sans-serif">
    <p>Hi Elena,</p>
    <p>We received a request to reset the password for elena.rossi@example.com.</p>
    <p>
      <a href="https://accounts.example.com/reset?token=Q9x2LmP7vK4tR8sW">Reset your password</a>
    </p>
    <p>This link expires at 18:45 UTC on 2 October 2026. If you didn't ask for this, you can ignore this email.</p>
    <p>— Example Accounts</p>
  </body>
</html>
`,
  },
  {
    id: "python",
    label: "Python settings module",
    group: "Data & code",
    text: `"""Settings for the billing worker."""

SERVICE_NAME = "billing-worker"
ENVIRONMENT = "production"

DATABASE = {
    "host": "pg-billing.internal.example.com",
    "port": 5432,
    "name": "billing",
    "user": "billing_rw",
}

KAFKA_BROKERS = ["10.8.0.11:9092", "10.8.0.12:9092", "10.8.0.13:9092"]
INVOICE_TOPIC = "billing.invoices.v2"
RETRY_LIMIT = 5
ALERT_EMAIL = "billing-oncall@example.com"
`,
  },
  {
    id: "csv",
    label: "CSV of user accounts (rows as a loop)",
    group: "Data & code",
    text: `username,email,team,role
akhan,aisha.khan@example.com,platform,admin
bnovak,bruno.novak@example.com,payments,developer
cmendes,carla.mendes@example.com,support,viewer
`,
  },
  // HashiCorp Vault: who may read which secrets, and how an app gets them.
  {
    id: "vault-policy",
    label: "HashiCorp Vault policy (HCL)",
    group: "Config & infrastructure",
    text: `# Policy for the orders service: read its own secrets, renew its token.
path "secret/data/orders/production/*" {
  capabilities = ["read", "list"]
}

path "database/creds/orders-readonly" {
  capabilities = ["read"]
}

path "transit/encrypt/orders-payments" {
  capabilities = ["update"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}
`,
  },
  {
    id: "vault-agent",
    label: "Vault Agent: fetch a database secret (HCL)",
    group: "Config & infrastructure",
    text: `pid_file = "/run/vault-agent/pidfile"

vault {
  address = "https://vault.acme.example.com:8200"
  ca_cert = "/etc/vault/ca.pem"
}

auto_auth {
  method "kubernetes" {
    mount_path = "auth/kubernetes-prod"
    config = {
      role = "orders-api"
    }
  }

  sink "file" {
    config = {
      path = "/home/vault/.vault-token"
    }
  }
}

template {
  destination = "/etc/orders/database.env"
  contents    = <<EOT
{{ with secret "database/creds/orders-readonly" }}
DB_USER={{ .Data.username }}
DB_PASSWORD={{ .Data.password }}
{{ end }}
EOT
  perms       = "0640"
}
`,
  },
  {
    id: "terraform-vault",
    label: "Terraform reading a secret from Vault",
    group: "Config & infrastructure",
    text: `provider "vault" {
  address   = "https://vault.acme.example.com:8200"
  namespace = "platform"
}

data "vault_kv_secret_v2" "orders_db" {
  mount = "secret"
  name  = "orders/production/database"
}

resource "kubernetes_secret" "orders_db" {
  metadata {
    name      = "orders-db"
    namespace = "orders"
  }

  data = {
    username = data.vault_kv_secret_v2.orders_db.data["username"]
    password = data.vault_kv_secret_v2.orders_db.data["password"]
  }
}
`,
  },
  // Other engines: these load with their engine selected in the output panel.
  {
    id: "cloud-init",
    label: "cloud-init user-data (Terraform templatefile, nested loops)",
    group: "Config & infrastructure",
    engine: "terraform",
    text: `#cloud-config
hostname: app-01
timezone: Europe/London

packages:
  - nginx
  - postgresql-client
  - fail2ban

users:
  - name: deploy
    groups: sudo
    shell: /bin/bash
    ssh_authorized_keys:
      - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK1x deploy@laptop
  - name: monitor
    groups: adm
    shell: /bin/bash
    ssh_authorized_keys:
      - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIM2y monitor@ops
      - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIN3z monitor@backup

runcmd:
  - systemctl enable --now nginx
`,
  },
  {
    id: "haproxy",
    label: "HAProxy backend pool (Terraform templatefile, server loop)",
    group: "Config & infrastructure",
    engine: "terraform",
    text: `frontend www
    bind *:443 ssl crt /etc/haproxy/certs/shop.example.com.pem
    default_backend shop

backend shop
    balance roundrobin
    option httpchk GET /healthz
    server web-01 10.0.1.11:8080 check
    server web-02 10.0.1.12:8080 check
    server web-03 10.0.1.13:8080 check
`,
  },
  {
    id: "nginx-envsubst",
    label: "nginx Docker image template (envsubst; $host stays nginx's)",
    group: "Config & infrastructure",
    engine: "envsubst",
    text: `server {
    listen 80;
    server_name shop.example.com;

    location / {
        proxy_pass http://storefront.internal.example.com:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /api/ {
        proxy_pass http://orders-api.internal.example.com:8080/;
        proxy_read_timeout 30s;
    }
}
`,
  },
];
