path "secret/data/payments/*" {
  capabilities = ["read", "list"]
}

path "database/creds/payments-ro" {
  capabilities = ["read"]
}
