from netbox.api.routers import NetBoxRouter

from . import views

app_name = "netbox_cloud_pilot"

router = NetBoxRouter()
router.register("configurations", views.NetBoxConfigurationViewSet)

urlpatterns = router.urls
