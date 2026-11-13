export const SAMPLE = `server {
    listen 443 ssl;
    server_name billing.example.com;

    ssl_certificate /etc/nginx/certs/billing.example.com.crt;

    location / {
        proxy_pass 10.20.30.41:8080;
        proxy_set_header X-Env production;
    }
}
`;

export const SAMPLE_VARIANT = `server {
    listen 443 ssl;
    server_name payments.example.com;

    ssl_certificate /etc/nginx/certs/payments.example.com.crt;

    location / {
        proxy_pass 10.20.30.77:8080;
        proxy_set_header X-Env staging;
    }
}
`;
