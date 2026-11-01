"""No proprietary membrane catalogs or fitted projection parameters are distributed."""

def validate_product_transport(**kwargs):
    raise NotImplementedError("Product-bound transport is not included in this release. Use the general WaterTAP unit API with explicit user-supplied transport parameters.")
