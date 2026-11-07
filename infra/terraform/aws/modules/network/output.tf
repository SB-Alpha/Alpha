output "vpc_id" {
  value = aws_vpc.this.id
}

output "vpc_name" {
  value = aws_vpc.this.tags["Name"]
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "subnet_cidrs" {
  value = local.public_cidrs
}

output "db_range_cidr" {
  value = local.db_range_cidr
}

output "alb_security_group_id" {
  value = aws_security_group.alb.id
}

output "app_security_group_id" {
  value = aws_security_group.app.id
}

output "db_security_group_id" {
  value = aws_security_group.db.id
}
