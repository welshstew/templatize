region          = "eu-west-2"
environment     = "staging"
vpc_cidr        = "10.42.0.0/16"
instance_type   = "t3.medium"
min_size        = 2
max_size        = 6
db_instance     = "db.r6g.large"
db_name         = "orders"
domain_name     = "staging.shop.example.com"
certificate_arn = "arn:aws:acm:eu-west-2:123456789012:certificate/8f2c1d4e-5a6b-4c7d-9e8f-0a1b2c3d4e5f"
tags = {
  Owner      = "platform-team"
  CostCenter = "CC-4410"
}
