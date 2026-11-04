module "registry" {
  source        = "./modules/registry"
  project_id    = var.project_id
  location      = var.region
  repository_id = "target-app"
}

module "network" {
  source     = "./modules/network"
  name       = var.service_name
  project_id = var.project_id
  region     = var.region
}