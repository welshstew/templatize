provider "aws" {
  region = "eu-central-1"
}

resource "aws_s3_bucket" "assets" {
  bucket = "acme-web-assets-prod"
}

resource "aws_instance" "bastion" {
  ami           = "ami-0a1b2c3d4e5f67890"
  instance_type = "t3.micro"
  subnet_id     = "subnet-0f1e2d3c4b5a69788"
  key_name      = "ops-2026"

  tags = {
    Name = "bastion-prod"
  }
}
