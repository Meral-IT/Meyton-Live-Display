const brand = document.querySelector(".brand");
const logo = brand.querySelector(".brand-logo");
const showLogo = () => brand.classList.toggle("custom-logo", logo.hasAttribute("src") && logo.naturalWidth > 0);
logo.addEventListener("load", showLogo);
logo.addEventListener("error", showLogo);
showLogo();

export function updateLogo(image) {
  if (!image) {
    logo.removeAttribute("src");
    brand.classList.remove("custom-logo");
  } else if (logo.src !== new URL(image.url, location.origin).href) {
    brand.classList.remove("custom-logo");
    logo.src = image.url;
  }
}
