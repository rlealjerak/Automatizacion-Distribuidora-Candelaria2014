output "name_servers" {
  description = "Set these at the registrar (Namecheap) in place of its default nameservers so this zone becomes authoritative."
  value       = aws_route53_zone.this.name_servers
}

output "zone_id" {
  value = aws_route53_zone.this.zone_id
}

output "certificate_arn" {
  value = aws_acm_certificate_validation.backend.certificate_arn
}

output "backend_hostname" {
  value = "${var.subdomain}.${var.domain_name}"
}
