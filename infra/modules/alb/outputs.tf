output "dns_name" {
  value = aws_lb.backend.dns_name
}

output "zone_id" {
  description = "ALB's own hosted zone ID, needed for a Route53 alias record (distinct from any Route53 zone we host)."
  value       = aws_lb.backend.zone_id
}

output "security_group_id" {
  value = aws_security_group.alb.id
}

output "target_group_arn" {
  value = aws_lb_target_group.backend.arn
}
