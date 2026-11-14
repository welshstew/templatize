source "amazon-ebs" "base" {
  region        = "eu-west-1"
  instance_type = "t3.small"
  source_ami    = "ami-0abcdef1234567890"
  ssh_username  = "ubuntu"
  ami_name      = "acme-base-2026-11"
}
