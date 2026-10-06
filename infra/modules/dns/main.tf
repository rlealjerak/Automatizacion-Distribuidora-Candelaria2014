############################################################
# DNS + TLS cert for the backend's public hostname.
#
# The domain itself (distribuidoracandelaria2014ops.com) is registered
# at Namecheap, not through Route53 - aws_route53domains isn't involved
# here at all. This module only creates the *hosted zone* (DNS records)
# and the ACM cert validated against it. For this hosted zone to be
# authoritative, the domain's nameservers at Namecheap must be pointed at
# this zone's `name_servers` output - a one-time manual step at the
# registrar, outside Terraform's reach. Until that NS delegation
# propagates, ACM's DNS validation (below) will sit in "Pending
# validation" - that's expected, not a bug.
############################################################

resource "aws_route53_zone" "this" {
  name = var.domain_name

  tags = {
    Name = "${var.project_name}-${var.environment}-zone"
  }
}

resource "aws_acm_certificate" "backend" {
  domain_name       = "${var.subdomain}.${var.domain_name}"
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name = "${var.project_name}-${var.environment}-backend-cert"
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.backend.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id         = aws_route53_zone.this.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "backend" {
  certificate_arn         = aws_acm_certificate.backend.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

resource "aws_route53_record" "backend_alias" {
  zone_id = aws_route53_zone.this.zone_id
  name    = "${var.subdomain}.${var.domain_name}"
  type    = "A"

  alias {
    name                   = var.alb_dns_name
    zone_id                = var.alb_zone_id
    evaluate_target_health = true
  }
}
