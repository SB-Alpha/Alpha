variable "project_id" {
  description = "GCP 프로젝트 ID"
  type        = string
}

variable "region" {
  description = "Cloud SQL 을 만들 리전"
  type        = string
}

variable "name" {
  description = "인스턴스 이름 접두사 (service_name)"
  type        = string
}

variable "network_id" {
  description = "Cloud SQL 이 사설 IP 로 연결할 VPC"
  type        = string
}

variable "db_version" {
  description = "PostgreSQL 메이저 버전"
  type        = string
}

variable "db_name" {
  description = "앱이 쓰는 DB 이름. postgres 이면 기본 DB 를 그대로 씀"
  type        = string
}

variable "db_user" {
  description = "앱이 쓰는 DB 사용자 이름"
  type        = string
}

variable "db_tier" {
  description = "Cloud SQL 머신 등급"
  type        = string
}

variable "db_disk_size_gb" {
  description = "디스크 크기 (GB)"
  type        = number
}

variable "db_logical_decoding" {
  description = "논리 복제(CDC) 사용 여부"
  type        = bool
}
