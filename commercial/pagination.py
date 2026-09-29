from rest_framework.pagination import PageNumberPagination


class CompanyPagination(PageNumberPagination):
    """Ten a page: the Companies dialog loads ten, then ten more as it scrolls."""

    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 50
