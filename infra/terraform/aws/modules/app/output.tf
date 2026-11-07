output "app_url" {
  value = "http://${aws_lb.this.dns_name}"
}
