module "registry" {
  source        = "./modules/registry"
  project_id    = var.project_id
  location      = var.region
  repository_id = "target-app"
}
