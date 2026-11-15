variable "cluster_name" {
  type    = string
  default = "orders-eks-prod"
}

variable "node_count" {
  type    = number
  default = 5
}

variable "allowed_cidrs" {
  type    = list(string)
  default = ["203.0.113.0/24", "198.51.100.0/24"]
}
