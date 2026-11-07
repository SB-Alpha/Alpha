output "app_url" {
  value = "http://${aws_lb.this.dns_name}"
}

output "alb_dns_name" {
  value = aws_lb.this.dns_name
}
